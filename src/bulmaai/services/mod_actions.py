"""The one path for moderation actions: slash commands, the panel, alert buttons, appeals, anti-raid and
the warn ladder all call perform(). It does the Discord side, records the case, DMs the user, posts the
case to the mod-log channel and, for warns, applies the warn ladder."""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import discord

from bulmaai.services import mod_cases
from bulmaai.ui.mod_views import appeal_view
from bulmaai.web.core import PERMISSIONS, Tier, resolve_member, tier_for

log = logging.getLogger(__name__)

MAX_TIMEOUT_SECONDS = 28 * 86400  # Discord's cap
WARN_REPEAT_SECONDS = 30
SOFTBAN_DELETE_SECONDS = 86400
DISCORD_ACTIONS = {"timeout", "untimeout", "kick", "ban", "softban", "unban"}
MEMBER_ONLY_ACTIONS = {"warn", "timeout", "untimeout", "kick"}
DM_ACTIONS = {"warn", "timeout", "kick", "ban", "softban"}
ACTION_COLORS = {
    "warn": discord.Color.gold(),
    "timeout": discord.Color.orange(),
    "untimeout": discord.Color.green(),
    "kick": discord.Color.dark_orange(),
    "softban": discord.Color.dark_orange(),
    "ban": discord.Color.red(),
    "unban": discord.Color.green(),
}


class ModActionError(Exception):
    """Refused or failed action; status maps onto the panel's HTTP error codes."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class LadderStepSkipped(ModActionError):
    """The warn ladder reached a step above the warner's own level."""


@dataclass(frozen=True)
class ActionResult:
    action: str
    case_id: int | None
    dm_sent: bool | None = None
    escalation: "ActionResult | None" = None
    expires_at: datetime | None = None
    duration_seconds: int | None = None
    ladder_skipped: str | None = None


# --- durations ---------------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"(\d+)\s*(seconds?|secs?|minutes?|mins?|months?|mos?|hours?|hrs?|weeks?|wks?|years?|yrs?|days?|[smhdwy])\b",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "wk": 604800, "wks": 604800, "w": 604800,
    "month": 2592000, "months": 2592000, "mo": 2592000, "mos": 2592000,
    "year": 31536000, "years": 31536000, "yr": 31536000, "yrs": 31536000, "y": 31536000,
}
_NO_DURATION = {"permanent", "indefinite", "forever", "n/a", "none"}


def parse_duration_seconds(text: str | None) -> int | None:
    """Best-effort parse of things like '1h', '30m', '2 days', '1 day, 2 hours'."""
    if not text:
        return None
    lowered = text.strip().lower()
    if lowered in _NO_DURATION:
        return None
    total = 0
    found = False
    for amount, unit in _DURATION_RE.findall(lowered):
        total += int(amount) * _UNIT_SECONDS[unit.lower()]
        found = True
    return total if found else None


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "permanent"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return " ".join(parts[:2])


# --- warn ladder -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LadderStep:
    warns: int
    window_seconds: int | None  # None = all time
    action: str  # timeout | kick | ban
    duration_seconds: int | None = None


_STEP_RE = re.compile(r"^\s*(\d+)\s*(?:/\s*([^=]+?))?\s*=\s*(.+?)\s*$")


def parse_ladder(text: str | None) -> tuple[LadderStep, ...]:
    """'2/7d=24h, 5/30d=3d, 7/30d=ban' -> steps sorted by warn count. Bad steps are logged and skipped."""
    steps = []
    for chunk in (text or "").split(","):
        if not chunk.strip():
            continue
        match = _STEP_RE.match(chunk)
        outcome = match[3].lower() if match else ""
        window = parse_duration_seconds(match[2]) if match and match[2] else None
        if match and outcome in ("kick", "ban"):
            steps.append(LadderStep(int(match[1]), window, outcome))
        elif match and (duration := parse_duration_seconds(outcome)):
            steps.append(LadderStep(int(match[1]), window, "timeout", min(duration, MAX_TIMEOUT_SECONDS)))
        else:
            log.warning("Ignoring bad moderation_warn_ladder step %r", chunk.strip())
    return tuple(sorted(steps, key=lambda step: step.warns))


def pick_step(steps: tuple[LadderStep, ...], counts: dict[int | None, int]) -> LadderStep | None:
    """counts maps window_seconds -> active warns in that window. The highest threshold met wins,
    so every warn past a threshold re-applies that step."""
    matched = [step for step in steps if counts.get(step.window_seconds, 0) >= step.warns]
    return max(matched, key=lambda step: step.warns, default=None)


async def escalate(
    bot: discord.Bot, guild: discord.Guild, user_id: int, warner: discord.Member | None = None
) -> ActionResult | None:
    """warner=None (automod) gets the whole ladder; a staff warn only runs steps the warner could take
    themselves per PERMISSIONS (timeouts and kicks for helpers); moderators get every step."""
    steps = parse_ladder(bot.settings.moderation_warn_ladder)
    if not steps:
        return None
    now = discord.utils.utcnow()
    counts = {}
    for window in {step.window_seconds for step in steps}:
        since = now - timedelta(seconds=window) if window else None
        counts[window] = await mod_cases.count_active_since(guild.id, user_id, "warn", since)
    step = pick_step(steps, counts)
    if step is None:
        return None
    needed = min(PERMISSIONS.get(f"mod.{step.action}", Tier.MODERATOR), Tier.MODERATOR)
    if warner is not None and tier_for(warner, bot.settings) < needed:
        raise LadderStepSkipped(f"ladder step {step.action} skipped: needs a {needed.name.lower()}")
    reason = f"Automatic: {counts[step.window_seconds]} warnings"
    if step.window_seconds:
        reason += f" in {format_duration(step.window_seconds)}"
    # ponytail: a re-applied timeout restarts from now, even if a longer one was running.
    return await perform(
        bot,
        guild,
        action=step.action,
        target_id=user_id,
        moderator=None,
        reason=reason,
        duration_seconds=step.duration_seconds,
        source="escalation",
    )


# --- checks, DMs, mod-log ----------------------------------------------------------------------


# ponytail: in-memory and per process, so a restart forgets recent warns; it only has to stop rapid stacking.
_recent_warns: dict[tuple[int, int], float] = {}


def _claim_warn(moderator_id: int, target_id: int) -> bool:
    """False when this moderator already warned this user in the last WARN_REPEAT_SECONDS."""
    now = time.monotonic()
    for key in [key for key, at in _recent_warns.items() if now - at >= WARN_REPEAT_SECONDS]:
        del _recent_warns[key]
    if (moderator_id, target_id) in _recent_warns:
        return False
    _recent_warns[(moderator_id, target_id)] = now
    return True


def check_hierarchy(
    bot: discord.Bot,
    guild: discord.Guild,
    moderator: discord.Member | None,
    target_id: int,
    target: discord.Member | None,
    *,
    discord_action: bool,
) -> None:
    """Raises ModActionError for actions staff shouldn't (or Discord won't) take.
    moderator=None means the bot acting on its own (automod, warn ladder, anti-raid)."""
    if moderator is not None and target_id == moderator.id:
        raise ModActionError("You can't moderate yourself.", 403)
    if target_id == guild.owner_id:
        raise ModActionError("You can't moderate the server owner.", 403)
    if bot.user is not None and target_id == bot.user.id:
        raise ModActionError("You can't moderate the bot.", 403)
    if target is None:
        return
    if moderator is not None:
        if tier_for(target, bot.settings) >= tier_for(moderator, bot.settings):
            raise ModActionError("That user's panel tier is equal to or above yours.", 403)
        if moderator.id != guild.owner_id and target.top_role.position >= moderator.top_role.position:
            raise ModActionError("That user's top role is equal to or above yours.", 403)
    if discord_action and target.top_role.position >= guild.me.top_role.position:
        raise ModActionError("The bot's top role isn't above that user's, so Discord won't allow it.", 409)


def dm_text(guild_name: str, action: str, reason: str, duration_seconds: int | None) -> str:
    what = {
        "warn": f"You were warned in **{guild_name}**.",
        "timeout": f"You were timed out in **{guild_name}** for {format_duration(duration_seconds)}.",
        "kick": f"You were kicked from **{guild_name}**.",
        "softban": f"You were kicked from **{guild_name}** and your recent messages were removed.",
        "ban": (
            f"You were banned from **{guild_name}** for {format_duration(duration_seconds)}."
            if duration_seconds
            else f"You were banned from **{guild_name}**."
        ),
    }[action]
    return f"{what}\nReason: {reason or 'No reason given'}"


async def _dm(member: discord.Member, text: str, view: discord.ui.View | None) -> bool:
    try:
        await member.send(text, view=view, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        return False
    return True


def case_embed(
    *,
    case_id: int | None,
    action: str,
    user_id: int,
    moderator_id: int | None,
    reason: str | None,
    duration_seconds: int | None = None,
    expires_at: datetime | None = None,
    source: str = "",
) -> discord.Embed:
    title = f"Case #{case_id} | {action.title()}" if case_id else action.title()
    embed = discord.Embed(
        title=title,
        color=ACTION_COLORS.get(action, discord.Color.blurple()),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="User", value=f"<@{user_id}> (`{user_id}`)", inline=True)
    embed.add_field(name="Moderator", value=f"<@{moderator_id}>" if moderator_id else "BulmaAI (automatic)", inline=True)
    if duration_seconds:
        embed.add_field(name="Duration", value=format_duration(duration_seconds), inline=True)
    if expires_at:
        embed.add_field(name="Expires", value=discord.utils.format_dt(expires_at, "R"), inline=True)
    embed.add_field(name="Reason", value=(reason or "No reason given")[:1024], inline=False)
    if source:
        embed.set_footer(text=f"via {source}")
    return embed


async def resolve_channel(bot: discord.Bot, channel_id: int | None) -> discord.abc.Messageable | None:
    if channel_id is None:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning("Can't reach channel %s", channel_id, exc_info=True)
            return None
    return channel if hasattr(channel, "send") else None


def mod_log_channel_id(settings) -> int | None:
    return settings.moderation_log_channel_id or settings.discord_log_channel_id


async def staff_channel(bot: discord.Bot, channel_id: int | None) -> discord.abc.Messageable | None:
    """A configured staff channel (appeals, reports), falling back to the moderation log."""
    return await resolve_channel(bot, channel_id or mod_log_channel_id(bot.settings))


async def post_case_log(bot: discord.Bot, **case: object) -> None:
    """Best effort; takes case_embed()'s keyword arguments."""
    try:
        channel = await resolve_channel(bot, mod_log_channel_id(bot.settings))
        if channel is not None:
            await channel.send(embed=case_embed(**case), allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.warning("Failed to post a case to the moderation log", exc_info=True)


# --- the action --------------------------------------------------------------------------------


async def perform(
    bot: discord.Bot,
    guild: discord.Guild,
    *,
    action: str,
    target_id: int,
    moderator: discord.Member | None,
    reason: str,
    duration_seconds: int | None = None,
    delete_message_seconds: int = 0,
    source: str = "command",
    notify: bool = True,
    log_case: bool = True,
    record: bool = True,
) -> ActionResult:
    """action: warn | note | timeout | untimeout | kick | ban | softban | unban.
    duration_seconds: required for timeout; makes a ban a tempban. Raises ModActionError.
    record=False leaves no case, mod-log post or "via" tag in Discord's audit log (the private VPS console)."""
    settings = bot.settings
    member = await resolve_member(guild, target_id)
    if member is None and action in MEMBER_ONLY_ACTIONS:
        raise ModActionError("That user isn't in the server.", 404)
    check_hierarchy(bot, guild, moderator, target_id, member, discord_action=action in DISCORD_ACTIONS)
    if action == "warn" and moderator is not None and not _claim_warn(moderator.id, target_id):
        raise ModActionError(f"You just warned them; wait {WARN_REPEAT_SECONDS}s before warning them again.", 409)

    if action == "timeout":
        if not duration_seconds or duration_seconds < 1:
            raise ModActionError("A timeout needs a duration.")
        duration_seconds = min(duration_seconds, MAX_TIMEOUT_SECONDS)
    elif action != "ban":
        duration_seconds = None
    expires_at = discord.utils.utcnow() + timedelta(seconds=duration_seconds) if action == "ban" and duration_seconds else None
    by = moderator.name if moderator is not None else "BulmaAI"
    audit_reason = (f"{reason or 'No reason given'} (via {source} by {by})" if record else reason or "")[:512] or None

    dm_sent = None
    if notify and settings.moderation_dm_on_action and member is not None and action in DM_ACTIONS:
        # Before kick/ban on purpose: once they share no server with the bot the DM can't be delivered.
        view = appeal_view(guild.id) if action == "ban" and settings.moderation_appeals_enabled else None
        dm_sent = await _dm(member, dm_text(guild.name, action, reason, duration_seconds), view)

    try:
        if action == "timeout":
            await member.timeout_for(timedelta(seconds=duration_seconds), reason=audit_reason)
        elif action == "untimeout":
            await member.remove_timeout(reason=audit_reason)
        elif action == "kick":
            await member.kick(reason=audit_reason)
        elif action in ("ban", "softban"):
            if action == "softban":
                delete_message_seconds = delete_message_seconds or SOFTBAN_DELETE_SECONDS
            await guild.ban(
                discord.Object(id=target_id), delete_message_seconds=delete_message_seconds, reason=audit_reason
            )
            if action == "softban":
                await guild.unban(discord.Object(id=target_id), reason=audit_reason)
        elif action == "unban":
            await guild.unban(discord.Object(id=target_id), reason=audit_reason)
    except discord.NotFound:
        raise ModActionError("That user isn't banned." if action == "unban" else "Discord couldn't find that user.", 404)
    except discord.Forbidden:
        raise ModActionError("Discord refused: the bot is missing permissions for that.", 409)
    except discord.HTTPException:
        log.exception("%s on %s failed", action, target_id)
        raise ModActionError("Discord returned an error, try again.", 502)

    moderator_id = moderator.id if moderator is not None else None
    try:
        if action == "unban":
            await mod_cases.deactivate_user_cases(guild.id, target_id, "ban")  # stops a pending tempban expiry
        if not record:
            return ActionResult(action=action, case_id=None, dm_sent=dm_sent, duration_seconds=duration_seconds)
        case_id = await mod_cases.record_case(
            guild_id=guild.id,
            user_id=target_id,
            moderator_id=moderator_id,
            action=action,
            reason=reason or None,
            duration_seconds=duration_seconds,
            source=source,
            expires_at=expires_at,
        )
    except Exception:
        log.exception("Failed to record mod case %s for %s", action, target_id)
        if action == "note":
            raise ModActionError("Couldn't save the note, the database is unavailable.", 503)
        case_id = None

    if log_case and action != "note":  # notes are staff-private, like Dyno's
        await post_case_log(
            bot,
            case_id=case_id,
            action=action,
            user_id=target_id,
            moderator_id=moderator_id,
            reason=reason,
            duration_seconds=duration_seconds,
            expires_at=expires_at,
            source=source,
        )

    escalation = None
    ladder_skipped = None
    if action == "warn":
        try:
            escalation = await escalate(bot, guild, target_id, moderator)
        except LadderStepSkipped as skipped:
            ladder_skipped = str(skipped)
            if case_id is not None:
                try:
                    note = f"{reason or 'No reason given'} ({ladder_skipped})"
                    await mod_cases.update_reason(guild.id, case_id, note)
                except Exception:
                    log.exception("Couldn't note the skipped ladder step on case %s", case_id)
        except ModActionError as error:
            log.warning("Warn ladder step for %s skipped: %s", target_id, error)
        except Exception:
            log.exception("Warn ladder failed for %s", target_id)
    return ActionResult(
        action=action,
        case_id=case_id,
        dm_sent=dm_sent,
        escalation=escalation,
        expires_at=expires_at,
        duration_seconds=duration_seconds,
        ladder_skipped=ladder_skipped,
    )


# --- channel locks -----------------------------------------------------------------------------


async def lock_channel(channel: discord.abc.GuildChannel, *, moderator_id: int | None, reason: str) -> None:
    """Deny @everyone sending; the previous overwrite is saved so unlock restores it exactly.
    Raises discord.HTTPException (Forbidden) like set_permissions does."""
    role = channel.guild.default_role
    overwrite = channel.overwrites_for(role)
    try:
        await mod_cases.save_lock(
            channel.guild.id,
            channel.id,
            prev_send=overwrite.send_messages,
            prev_send_threads=overwrite.send_messages_in_threads,
            locked_by=moderator_id,
        )
    except Exception:
        log.exception("Couldn't save lock state for %s; unlocking will fall back to inherited permissions", channel.id)
    overwrite.update(send_messages=False, send_messages_in_threads=False)
    await channel.set_permissions(role, overwrite=overwrite, reason=reason)


async def unlock_channel(channel: discord.abc.GuildChannel, *, reason: str) -> None:
    role = channel.guild.default_role
    try:
        previous = await mod_cases.pop_lock(channel.id)
    except Exception:
        log.exception("Couldn't read lock state for %s; restoring inherited permissions", channel.id)
        previous = None
    prev_send, prev_threads = previous or (None, None)
    overwrite = channel.overwrites_for(role)
    overwrite.update(send_messages=prev_send, send_messages_in_threads=prev_threads)
    await channel.set_permissions(role, overwrite=None if overwrite.is_empty() else overwrite, reason=reason)


def lockdown_targets(guild: discord.Guild, settings) -> list[discord.abc.GuildChannel]:
    if settings.moderation_lockdown_channel_ids:
        channels = (guild.get_channel(channel_id) for channel_id in settings.moderation_lockdown_channel_ids)
        return [channel for channel in channels if channel is not None]
    role = guild.default_role
    return [channel for channel in guild.text_channels if channel.permissions_for(role).send_messages]


async def lockdown(guild: discord.Guild, settings, *, moderator_id: int | None, reason: str) -> tuple[int, int]:
    """(locked, failed)."""
    locked = failed = 0
    for channel in lockdown_targets(guild, settings):
        try:
            await lock_channel(channel, moderator_id=moderator_id, reason=reason)
            locked += 1
        except discord.HTTPException:
            log.warning("Lockdown couldn't lock #%s", getattr(channel, "name", channel.id), exc_info=True)
            failed += 1
    return locked, failed


async def end_lockdown(guild: discord.Guild, *, reason: str) -> tuple[int, int]:
    """Unlocks every channel we have a lock row for (including single /lock ones). (unlocked, failed)."""
    unlocked = failed = 0
    for channel_id in await mod_cases.locked_channel_ids(guild.id):
        channel = guild.get_channel(channel_id)
        if channel is None:
            await mod_cases.pop_lock(channel_id)  # channel deleted meanwhile
            continue
        try:
            await unlock_channel(channel, reason=reason)
            unlocked += 1
        except discord.HTTPException:
            log.warning("Couldn't unlock #%s", getattr(channel, "name", channel_id), exc_info=True)
            failed += 1
    return unlocked, failed
