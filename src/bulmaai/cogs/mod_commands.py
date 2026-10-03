"""Dyno-style moderation slash commands and context menus. Every action goes through mod_actions.perform();
access follows the panel's staff tiers (web/core.py PERMISSIONS), default_permissions only hides the commands."""

import asyncio
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from bulmaai.services import mod_actions, mod_cases, scam_images
from bulmaai.services.mod_actions import (
    MAX_TIMEOUT_SECONDS,
    ActionResult,
    LadderStep,
    ModActionError,
    format_duration,
    parse_duration_seconds,
    parse_ladder,
    pick_step,
)
from bulmaai.web.core import PERMISSIONS, resolve_member, tier_for

log = logging.getLogger(__name__)

MAX_REASON_LENGTH = 400  # the panel's cap; leaves room for the audit-log suffix under Discord's 512
BAN_DELETE_SECONDS = {"none": 0, "1h": 3600, "24h": 86400, "7d": 7 * 86400}
DANGEROUS_ROLE_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "ban_members",
    "kick_members",
    "moderate_members",
    "mention_everyone",
    "manage_messages",
    "manage_channels",
    "manage_nicknames",
    "manage_webhooks",
    "manage_threads",
    "view_audit_log",
)
PROTECTED_ROLE_SETTINGS = (
    "discord_staff_role_ids",
    "panel_owner_role_ids",
    "panel_admin_role_ids",
    "panel_moderator_role_ids",
    "panel_helper_role_ids",
    "patreon_access_role_ids",
    "dev_jar_patreon_role_ids",
    "dev_jar_tester_role_ids",
)
STAFF_ONLY = discord.Permissions(moderate_members=True)
VERBS = {
    "warn": "Warned",
    "note": "Added a note to",
    "timeout": "Timed out",
    "untimeout": "Removed the timeout from",
    "kick": "Kicked",
    "ban": "Banned",
    "softban": "Softbanned",
    "unban": "Unbanned",
}

_LINK_RE = re.compile(r"https?://", re.IGNORECASE)
_INVITE_RE = re.compile(r"discord(?:\.gg|(?:app)?\.com/invite)/", re.IGNORECASE)
PURGE_KINDS: dict[str, Callable[[discord.Message], bool]] = {
    "bots": lambda m: m.author.bot,
    "humans": lambda m: not m.author.bot,
    "links": lambda m: bool(_LINK_RE.search(m.content)),
    "images": lambda m: any((a.content_type or "").startswith("image/") for a in m.attachments),
    "attachments": lambda m: bool(m.attachments),
    "embeds": lambda m: bool(m.embeds),
    "invites": lambda m: bool(_INVITE_RE.search(m.content)),
}


def purge_check(
    *, user_id: int | None = None, contains: str | None = None, kind: str | None = None
) -> Callable[[discord.Message], bool]:
    """All filters ANDed; pinned messages are always kept."""
    needle = (contains or "").lower()
    matches_kind = PURGE_KINDS[kind] if kind else lambda _message: True

    def check(message: discord.Message) -> bool:
        return (
            not message.pinned
            and (user_id is None or message.author.id == user_id)
            and needle in message.content.lower()
            and matches_kind(message)
        )

    return check


def role_refusal(role: discord.Role, moderator: discord.Member, guild: discord.Guild, settings) -> str | None:
    if role.managed or role.is_default():
        return "That role is managed by Discord or an integration."
    if any(getattr(role.permissions, name) for name in DANGEROUS_ROLE_PERMISSIONS):
        return "Roles with moderation or server-management permissions can't be changed with /role."
    if any(role.id in getattr(settings, name) for name in PROTECTED_ROLE_SETTINGS):
        return "Staff, panel, Patreon and tester roles can't be changed with /role."
    if moderator.id != guild.owner_id and role.position >= moderator.top_role.position:
        return "That role is equal to or above your top role."
    if role.position >= guild.me.top_role.position:
        return "That role is equal to or above the bot's top role, so Discord won't allow it."
    return None


def _step_text(step: LadderStep) -> str:
    return f"timeout {format_duration(step.duration_seconds)}" if step.action == "timeout" else step.action


def _case_lines(cases: list[mod_cases.ModCase]) -> str:
    lines = []
    for case in cases:
        reason = (case.reason or "no reason").replace("\n", " ")
        reason = discord.utils.escape_markdown(reason if len(reason) <= 60 else reason[:59] + "…")
        line = f"`#{case.id}` **{case.action}** · {case.source} · {discord.utils.format_dt(case.created_at, 'R')} · {reason}"
        lines.append(line if case.active else f"~~{line}~~")
    return "\n".join(lines)[:4096]


class ReasonModal(discord.ui.Modal):
    def __init__(self, title: str, submit: Callable[[discord.Interaction, str], Awaitable[object]]):
        super().__init__(title=title[:45])
        self.reason_input = discord.ui.InputText(label="Reason", style=discord.InputTextStyle.long, max_length=300)
        self.add_item(self.reason_input)
        self._submit = submit

    async def callback(self, interaction: discord.Interaction):
        await self._submit(interaction, self.reason_input.value.strip())


class ModCommandsCog(commands.Cog):
    lockdown_group = discord.SlashCommandGroup(
        "lockdown", "Lock or unlock every public channel", default_member_permissions=STAFF_ONLY
    )
    role_group = discord.SlashCommandGroup("role", "Give or take a member's role", default_member_permissions=STAFF_ONLY)
    scamimage_group = discord.SlashCommandGroup(
        "scamimage", "Manage known scam images", default_member_permissions=STAFF_ONLY
    )

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._import_task: asyncio.Task | None = None

    async def _allowed(self, ctx: discord.ApplicationContext, capability: str) -> bool:
        if tier_for(ctx.user, self.bot.settings) >= PERMISSIONS[capability]:
            return True
        await ctx.respond("Your staff tier can't do that.", ephemeral=True)
        return False

    async def _perform(self, ctx, action: str, target_id: int, reason: str, **options) -> ActionResult | None:
        """ctx is an ApplicationContext or a modal's Interaction; replies to it either way."""
        # DM + Discord call + DB + mod-log post (+ a ladder step) can outlast the 3s interaction window.
        if not ctx.response.is_done():
            await ctx.response.defer(ephemeral=True)
        try:
            result = await mod_actions.perform(
                self.bot, ctx.guild, action=action, target_id=target_id, moderator=ctx.user, reason=reason, **options
            )
        except ModActionError as error:
            await ctx.respond(str(error), ephemeral=True)
            return None
        text = f"{VERBS[action]} <@{target_id}>"
        if options.get("duration_seconds"):
            text += f" for {format_duration(options['duration_seconds'])}"
        text += f" (case #{result.case_id})." if result.case_id else " (the case couldn't be recorded)."
        if result.dm_sent is not None:
            text += " DM delivered." if result.dm_sent else " Couldn't DM them."
        if escalation := result.escalation:
            duration = escalation.duration_seconds
            text += f"\n→ auto {escalation.action}" + (f" {format_duration(duration)}" if duration else "")
            text += f" (case #{escalation.case_id})" if escalation.case_id else ""
        await ctx.respond(text, ephemeral=True)
        return result

    # --- warnings and notes ------------------------------------------------------------------

    @discord.slash_command(name="warn", description="Warn a member (counts toward the warn ladder)")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to warn")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    async def warn(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.warn"):
            await self._perform(ctx, "warn", user.id, reason)

    @discord.slash_command(name="warnings", description="A member's active warnings and warn-ladder standing")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to look up")
    async def warnings(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        guild_id = ctx.guild.id
        cases = await mod_cases.list_cases(guild_id, user_id=user.id, action="warn", limit=100)
        warns = [case for case in cases if case.active]
        embed = discord.Embed(
            title=f"Warnings for {user}",
            description=_case_lines(warns[:25]) or "No active warnings.",
            color=discord.Color.gold(),
        )
        steps = parse_ladder(self.bot.settings.moderation_warn_ladder)
        if steps:
            now = discord.utils.utcnow()
            counts = {
                window: await mod_cases.count_active_since(
                    guild_id, user.id, "warn", now - timedelta(seconds=window) if window else None
                )
                for window in {step.window_seconds for step in steps}
            }
            lines = [
                f"{counts[step.window_seconds]}/{step.warns} in "
                f"{format_duration(step.window_seconds) if step.window_seconds else 'all time'} → {_step_text(step)}"
                for step in steps
            ]
            upcoming = pick_step(steps, {window: count + 1 for window, count in counts.items()})
            lines.append(f"Next warn: {_step_text(upcoming) if upcoming else 'no automatic action'}")
            embed.add_field(name="Warn ladder", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"{len(warns)} active warning(s)")
        await ctx.respond(embed=embed, ephemeral=True)

    async def _remove_case(self, ctx: discord.ApplicationContext, case_id: int, action: str) -> None:
        if not await self._allowed(ctx, "mod.cases.edit"):
            return
        label = "warning" if action == "warn" else action
        case = await mod_cases.get_case(ctx.guild.id, case_id)
        if case is None or case.action != action:
            return await ctx.respond(f"Case #{case_id} isn't a {label}.", ephemeral=True)
        if await mod_cases.deactivate_case(ctx.guild.id, case_id) is None:
            return await ctx.respond(f"Case #{case_id} was already removed.", ephemeral=True)
        await ctx.respond(f"Removed {label} #{case_id} for <@{case.user_id}>.", ephemeral=True)

    @discord.slash_command(name="delwarn", description="Remove a warning (it stops counting toward the ladder)")
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, description="Warning case number", min_value=1)
    async def delwarn(self, ctx: discord.ApplicationContext, case_id: int):
        await self._remove_case(ctx, case_id, "warn")

    @discord.slash_command(name="delnote", description="Remove a staff note")
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, description="Note case number", min_value=1)
    async def delnote(self, ctx: discord.ApplicationContext, case_id: int):
        await self._remove_case(ctx, case_id, "note")

    @discord.slash_command(name="clearwarns", description="Remove all of a member's active warnings")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member whose warnings to clear")
    async def clearwarns(self, ctx: discord.ApplicationContext, user: discord.User):
        if await self._allowed(ctx, "mod.cases.edit"):
            count = await mod_cases.deactivate_user_cases(ctx.guild.id, user.id, "warn")
            await ctx.respond(f"Cleared {count} warning(s) for <@{user.id}>.", ephemeral=True)

    @discord.slash_command(name="note", description="Add a staff-only note to a user")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to note")
    @discord.option("text", str, description="The note", max_length=MAX_REASON_LENGTH)
    async def note(self, ctx: discord.ApplicationContext, user: discord.User, text: str):
        if await self._allowed(ctx, "mod.warn"):
            await self._perform(ctx, "note", user.id, text)

    @discord.slash_command(name="notes", description="A user's staff notes")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to look up")
    async def notes(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        cases = await mod_cases.list_cases(ctx.guild.id, user_id=user.id, action="note", limit=100)
        notes = [case for case in cases if case.active]
        embed = discord.Embed(title=f"Notes for {user}", description=_case_lines(notes[:25]) or "No notes.")
        await ctx.respond(embed=embed, ephemeral=True)

    # --- actions -----------------------------------------------------------------------------

    @discord.slash_command(name="mute", description="Time a member out")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to time out")
    @discord.option("duration", str, description="e.g. 30m, 2h, 1d (max 28d)")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    async def mute(self, ctx: discord.ApplicationContext, user: discord.User, duration: str, reason: str):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        seconds = parse_duration_seconds(duration)
        if not seconds:
            return await ctx.respond("Couldn't read that duration, try 30m, 2h or 1d.", ephemeral=True)
        await self._perform(ctx, "timeout", user.id, reason, duration_seconds=min(seconds, MAX_TIMEOUT_SECONDS))

    @discord.slash_command(name="unmute", description="Remove a member's timeout")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to un-time-out")
    @discord.option("reason", str, description="Optional", max_length=MAX_REASON_LENGTH, required=False)
    async def unmute(self, ctx: discord.ApplicationContext, user: discord.User, reason: str = ""):
        if await self._allowed(ctx, "mod.timeout"):
            await self._perform(ctx, "untimeout", user.id, reason or "")

    @discord.slash_command(name="kick", description="Kick a member")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to kick")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    async def kick(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.kick"):
            await self._perform(ctx, "kick", user.id, reason)

    @discord.slash_command(name="ban", description="Ban a user, optionally for a limited time")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to ban (IDs of non-members work)")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    @discord.option("duration", str, description="e.g. 12h, 7d, 2w; empty = permanent", required=False)
    @discord.option("delete_messages", str, description="Delete their recent messages", choices=list(BAN_DELETE_SECONDS))
    async def ban(
        self,
        ctx: discord.ApplicationContext,
        user: discord.User,
        reason: str,
        duration: str = None,
        delete_messages: str = "none",
    ):
        if not await self._allowed(ctx, "mod.ban"):
            return
        seconds = parse_duration_seconds(duration) if duration else None
        if duration and not seconds:
            return await ctx.respond(
                "Couldn't read that duration, try 12h, 7d or 2w (leave it empty for a permanent ban).", ephemeral=True
            )
        await self._perform(
            ctx,
            "ban",
            user.id,
            reason,
            duration_seconds=seconds,
            delete_message_seconds=BAN_DELETE_SECONDS[delete_messages],
        )

    @discord.slash_command(name="softban", description="Ban and unban to delete a day of messages")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to softban")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    async def softban(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.ban"):
            await self._perform(ctx, "softban", user.id, reason)

    @discord.slash_command(name="unban", description="Unban a user by ID")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user_id", str, description="Their Discord user ID")
    @discord.option("reason", str, description="Why", max_length=MAX_REASON_LENGTH)
    async def unban(self, ctx: discord.ApplicationContext, user_id: str, reason: str):
        if not await self._allowed(ctx, "mod.ban"):
            return
        user_id = user_id.strip().strip("<@!>")
        if not re.fullmatch(r"[0-9]{15,20}", user_id):
            return await ctx.respond("That isn't a valid user ID.", ephemeral=True)
        await self._perform(ctx, "unban", int(user_id), reason)

    @discord.slash_command(name="purge", description="Bulk-delete recent messages in this channel")
    @discord.default_permissions(moderate_members=True)
    @discord.option("amount", int, description="How many recent messages to scan", min_value=1, max_value=500)
    @discord.option("user", discord.User, description="Only this user's messages", required=False)
    @discord.option("contains", str, description="Only messages containing this text", required=False)
    @discord.option("kind", str, description="Only this kind of message", choices=list(PURGE_KINDS), required=False)
    async def purge(
        self,
        ctx: discord.ApplicationContext,
        amount: int,
        user: discord.User = None,
        contains: str = None,
        kind: str = None,
    ):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        await ctx.defer(ephemeral=True)
        try:
            deleted = await ctx.channel.purge(
                limit=amount,
                check=purge_check(user_id=user.id if user else None, contains=contains, kind=kind),
                after=discord.utils.utcnow() - timedelta(days=14),  # older ones can't be bulk-deleted
                reason=f"/purge by {ctx.user.name}",
            )
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot can't delete messages here.", ephemeral=True)
        await ctx.respond(f"Deleted {len(deleted)} message(s) from the last 14 days.", ephemeral=True)
        if not deleted:
            return
        filters = [f"from <@{user.id}>" if user else "", f'containing "{contains}"' if contains else "", kind or ""]
        embed = discord.Embed(title="Purge", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
        embed.add_field(name="Moderator", value=ctx.user.mention, inline=True)
        embed.add_field(name="Deleted", value=str(len(deleted)), inline=True)
        if any(filters):
            embed.add_field(name="Filters", value=", ".join(f for f in filters if f)[:1024], inline=False)
        try:
            channel = await mod_actions.resolve_channel(self.bot, mod_actions.mod_log_channel_id(self.bot.settings))
            if channel is not None:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Failed to post a purge to the moderation log", exc_info=True)

    # --- cases -------------------------------------------------------------------------------

    @discord.slash_command(name="case", description="Show a moderation case")
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, description="Case number", min_value=1)
    async def case_show(self, ctx: discord.ApplicationContext, case_id: int):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        case = await mod_cases.get_case(ctx.guild.id, case_id)
        if case is None:
            return await ctx.respond("Unknown case.", ephemeral=True)
        embed = mod_actions.case_embed(
            case_id=case.id,
            action=case.action,
            user_id=case.user_id,
            moderator_id=case.moderator_id,
            reason=case.reason,
            duration_seconds=case.duration_seconds,
            expires_at=case.expires_at,
            source=case.source,
        )
        embed.timestamp = case.created_at
        if not case.active:
            embed.title += " (inactive)"
        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(name="reason", description="Change a case's reason")
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, description="Case number", min_value=1)
    @discord.option("reason", str, description="New reason", max_length=MAX_REASON_LENGTH)
    async def case_reason(self, ctx: discord.ApplicationContext, case_id: int, reason: str):
        if not await self._allowed(ctx, "mod.cases.edit"):
            return
        if not await mod_cases.update_reason(ctx.guild.id, case_id, reason.strip()):
            return await ctx.respond("Unknown case.", ephemeral=True)
        await ctx.respond(f"Updated the reason for case #{case_id}.", ephemeral=True)

    @discord.slash_command(name="modlogs", description="A user's last 25 moderation cases")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to look up")
    async def modlogs(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        cases = await mod_cases.list_cases(ctx.guild.id, user_id=user.id, limit=25)
        embed = discord.Embed(title=f"Mod logs for {user}", description=_case_lines(cases) or "No cases.")
        embed.set_footer(text="Struck-through cases are inactive (removed, cleared or expired).")
        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(name="modstats", description="Moderation actions per moderator")
    @discord.default_permissions(moderate_members=True)
    @discord.option("days", int, description="Look back this many days (default 30)", min_value=1, max_value=3650)
    async def modstats(self, ctx: discord.ApplicationContext, days: int = 30):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        since = discord.utils.utcnow() - timedelta(days=days)
        per_moderator: dict[int, Counter] = defaultdict(Counter)
        for moderator_id, action, count in await mod_cases.moderator_stats(ctx.guild.id, since):
            per_moderator[moderator_id][action] += count
        ranked = sorted(per_moderator.items(), key=lambda item: item[1].total(), reverse=True)[:25]
        lines = [
            f"<@{moderator_id}> **{counts.total()}** · " + ", ".join(f"{action} {n}" for action, n in counts.most_common())
            for moderator_id, counts in ranked
        ]
        embed = discord.Embed(
            title=f"Moderator stats, last {days} days",
            description="\n".join(lines)[:4096] or "No moderation in that period.",
        )
        await ctx.respond(embed=embed, ephemeral=True)

    # --- channels ----------------------------------------------------------------------------

    @discord.slash_command(name="lock", description="Stop @everyone from talking in a channel")
    @discord.default_permissions(moderate_members=True)
    @discord.option("channel", discord.TextChannel, description="Default: this channel", required=False)
    @discord.option("reason", str, description="Optional", max_length=MAX_REASON_LENGTH, required=False)
    async def lock(self, ctx: discord.ApplicationContext, channel: discord.TextChannel = None, reason: str = ""):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        channel = channel or ctx.channel
        if isinstance(channel, discord.Thread):
            return await ctx.respond("Threads can't be locked this way; lock the parent channel.", ephemeral=True)
        try:
            await mod_actions.lock_channel(
                channel, moderator_id=ctx.user.id, reason=f"{reason or 'No reason given'} (via command by {ctx.user.name})"
            )
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot can't edit that channel's permissions.", ephemeral=True)
        await ctx.respond(f"Locked {channel.mention}.", ephemeral=True)

    @discord.slash_command(name="unlock", description="Undo /lock on a channel")
    @discord.default_permissions(moderate_members=True)
    @discord.option("channel", discord.TextChannel, description="Default: this channel", required=False)
    async def unlock(self, ctx: discord.ApplicationContext, channel: discord.TextChannel = None):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        channel = channel or ctx.channel
        if isinstance(channel, discord.Thread):
            return await ctx.respond("Threads can't be unlocked this way; unlock the parent channel.", ephemeral=True)
        try:
            await mod_actions.unlock_channel(channel, reason=f"Unlocked (via command by {ctx.user.name})")
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot can't edit that channel's permissions.", ephemeral=True)
        await ctx.respond(f"Unlocked {channel.mention}.", ephemeral=True)

    @lockdown_group.command(name="start", description="Lock every public channel (or the configured list)")
    @discord.option("reason", str, description="Why", max_length=MAX_REASON_LENGTH)
    async def lockdown_start(self, ctx: discord.ApplicationContext, reason: str):
        if not await self._allowed(ctx, "mod.kick"):
            return
        await ctx.defer(ephemeral=True)
        locked, failed = await mod_actions.lockdown(
            ctx.guild, self.bot.settings, moderator_id=ctx.user.id, reason=f"{reason} (via command by {ctx.user.name})"
        )
        await ctx.respond(f"Locked {locked} channel(s)" + (f", {failed} failed." if failed else "."), ephemeral=True)

    @lockdown_group.command(name="end", description="Unlock every channel the bot locked")
    async def lockdown_end(self, ctx: discord.ApplicationContext):
        if not await self._allowed(ctx, "mod.kick"):
            return
        await ctx.defer(ephemeral=True)
        unlocked, failed = await mod_actions.end_lockdown(
            ctx.guild, reason=f"Lockdown ended (via command by {ctx.user.name})"
        )
        await ctx.respond(f"Unlocked {unlocked} channel(s)" + (f", {failed} failed." if failed else "."), ephemeral=True)

    @discord.slash_command(name="slowmode", description="Set a channel's slowmode")
    @discord.default_permissions(moderate_members=True)
    @discord.option("seconds", int, description="0 turns it off (max 21600 = 6h)", min_value=0, max_value=21600)
    @discord.option("channel", discord.TextChannel, description="Default: this channel", required=False)
    async def slowmode(self, ctx: discord.ApplicationContext, seconds: int, channel: discord.TextChannel = None):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        channel = channel or ctx.channel
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"/slowmode by {ctx.user.name}")
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot can't edit that channel.", ephemeral=True)
        await ctx.respond(
            f"Slowmode in {channel.mention} is now {format_duration(seconds) if seconds else 'off'}.", ephemeral=True
        )

    # --- members -----------------------------------------------------------------------------

    @discord.slash_command(name="whois", description="Account info, roles and case counts for a user")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to look up")
    async def whois(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        member = await resolve_member(ctx.guild, user.id)
        embed = discord.Embed(title=str(user), description=None if member else "Not in the server.")
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="ID", value=f"`{user.id}`", inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(user.created_at, "R"), inline=True)
        if member is not None:
            if member.joined_at:
                embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
            if member.timed_out:
                until = discord.utils.format_dt(member.communication_disabled_until, "R")
                embed.add_field(name="Timed out", value=f"until {until}", inline=True)
            roles = " ".join(role.mention for role in reversed(member.roles) if not role.is_default()) or "None"
            if len(roles) > 1024:
                roles = roles[:1000].rsplit(" ", 1)[0] + " …"
            embed.add_field(name="Roles", value=roles, inline=False)
        counts = Counter(case.action for case in await mod_cases.list_cases(ctx.guild.id, user_id=user.id, limit=1000))
        embed.add_field(
            name="Cases", value=", ".join(f"{action} {n}" for action, n in counts.most_common()) or "None", inline=False
        )
        await ctx.respond(embed=embed, ephemeral=True)

    async def _change_role(self, ctx: discord.ApplicationContext, user: discord.User, role: discord.Role, *, add: bool):
        if not await self._allowed(ctx, "mod.kick"):
            return
        member = await resolve_member(ctx.guild, user.id)
        if member is None:
            return await ctx.respond("That user isn't in the server.", ephemeral=True)
        refusal = role_refusal(role, ctx.user, ctx.guild, self.bot.settings)
        if refusal is None:
            try:
                mod_actions.check_hierarchy(self.bot, ctx.guild, ctx.user, user.id, member, discord_action=True)
            except ModActionError as error:
                refusal = str(error)
        if refusal:
            return await ctx.respond(refusal, ephemeral=True)
        try:
            if add:
                await member.add_roles(role, reason=f"/role add by {ctx.user.name}")
            else:
                await member.remove_roles(role, reason=f"/role remove by {ctx.user.name}")
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot couldn't change that role.", ephemeral=True)
        await ctx.respond(f"{'Gave' if add else 'Removed'} {role.mention} {'to' if add else 'from'} <@{user.id}>.", ephemeral=True)

    @role_group.command(name="add", description="Give a member a role")
    @discord.option("user", discord.User, description="Member")
    @discord.option("role", discord.Role, description="Role to give")
    async def role_add(self, ctx: discord.ApplicationContext, user: discord.User, role: discord.Role):
        await self._change_role(ctx, user, role, add=True)

    @role_group.command(name="remove", description="Take a role from a member")
    @discord.option("user", discord.User, description="Member")
    @discord.option("role", discord.Role, description="Role to take")
    async def role_remove(self, ctx: discord.ApplicationContext, user: discord.User, role: discord.Role):
        await self._change_role(ctx, user, role, add=False)

    @discord.slash_command(name="nick", description="Change or reset a member's nickname")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member")
    @discord.option("nickname", str, description="Empty resets it", max_length=32, required=False)
    async def nick(self, ctx: discord.ApplicationContext, user: discord.User, nickname: str = None):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        member = await resolve_member(ctx.guild, user.id)
        if member is None:
            return await ctx.respond("That user isn't in the server.", ephemeral=True)
        try:
            mod_actions.check_hierarchy(self.bot, ctx.guild, ctx.user, user.id, member, discord_action=True)
            await member.edit(nick=nickname or None, reason=f"/nick by {ctx.user.name}")
        except ModActionError as error:
            return await ctx.respond(str(error), ephemeral=True)
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot couldn't change that nickname.", ephemeral=True)
        await ctx.respond(f"{'Set' if nickname else 'Reset'} <@{user.id}>'s nickname.", ephemeral=True)

    @discord.slash_command(name="modimport-dyno", description="Import the full Dyno mod-log history into the case log")
    @discord.default_permissions(moderate_members=True)
    async def modimport_dyno(self, ctx: discord.ApplicationContext):
        if not await self._allowed(ctx, "settings.edit"):
            return
        sync = self.bot.get_cog("ModLogSyncCog")
        if sync is None or self.bot.settings.dyno_modlog_channel_id is None:
            return await ctx.respond("The Dyno mod-log sync isn't set up (cog or channel missing).", ephemeral=True)
        if self._import_task is not None and not self._import_task.done():
            return await ctx.respond("An import is already running.", ephemeral=True)
        self._import_task = asyncio.create_task(sync._backfill_dyno_modlog(self.bot.settings, limit=None))
        await ctx.respond(
            "Importing the whole Dyno mod-log channel in the background. Already-imported cases are skipped, "
            "so re-running is safe.",
            ephemeral=True,
        )

    # --- context menus -----------------------------------------------------------------------

    @discord.user_command(name="Mod logs")
    @discord.default_permissions(moderate_members=True)
    async def modlogs_menu(self, ctx: discord.ApplicationContext, user: discord.User):
        await self.modlogs.callback(self, ctx, user)

    @discord.user_command(name="Warn")
    @discord.default_permissions(moderate_members=True)
    async def warn_menu(self, ctx: discord.ApplicationContext, user: discord.User):
        if await self._allowed(ctx, "mod.warn"):
            await ctx.send_modal(
                ReasonModal(f"Warn {user.name}", lambda interaction, reason: self._perform(interaction, "warn", user.id, reason))
            )

    @discord.message_command(name="Delete & warn")
    @discord.default_permissions(moderate_members=True)
    async def delete_warn_menu(self, ctx: discord.ApplicationContext, message: discord.Message):
        if not await self._allowed(ctx, "mod.warn"):
            return
        excerpt = (message.content or "[no text]").replace("\n", " ")
        excerpt = excerpt if len(excerpt) <= 80 else excerpt[:79] + "…"

        async def submit(interaction: discord.Interaction, reason: str):
            # Warn first: if the hierarchy check refuses, the message stays.
            if await self._perform(interaction, "warn", message.author.id, f'{reason} (message: "{excerpt}")') is None:
                return
            try:
                await message.delete(reason=f"Delete & warn by {interaction.user.name}")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                await interaction.respond("Warned, but couldn't delete the message.", ephemeral=True)

        await ctx.send_modal(ReasonModal("Delete & warn", submit))

    @discord.message_command(name="Mark as scam image")
    @discord.default_permissions(moderate_members=True)
    async def mark_scam_image_menu(self, ctx: discord.ApplicationContext, message: discord.Message):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        await ctx.defer(ephemeral=True)
        hashes = [
            value
            for value in await asyncio.gather(
                *(scam_images.hash_attachment(a) for a in message.attachments if scam_images.is_hashable(a))
            )
            if value is not None
        ]
        if not hashes:
            return await ctx.respond("No usable images on that message.", ephemeral=True)
        try:
            hash_ids = [
                await scam_images.add(value, source="manual", added_by=ctx.user.id, note=f"message {message.id} by {message.author}")
                for value in hashes
            ]
        except Exception:
            log.exception("Couldn't save scam images from message %s", message.id)
            return await ctx.respond("Couldn't save, the database is unavailable.", ephemeral=True)
        deleted = "deleted the message"
        try:
            await message.delete(reason=f"Marked as a scam image by {ctx.user.name}")
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("Couldn't delete message %s after marking it as a scam image", message.id, exc_info=True)
            deleted = "couldn't delete the message"
        ids = ", ".join(f"#{hash_id}" for hash_id in hash_ids)
        await ctx.respond(f"Added scam image {ids} and {deleted}.", ephemeral=True)

    # --- scam images -------------------------------------------------------------------------

    @scamimage_group.command(name="list", description="Show the last 25 known scam image hashes")
    async def scamimage_list(self, ctx: discord.ApplicationContext):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        hashes = await scam_images.list_hashes(25)
        if not hashes:
            return await ctx.respond("No known scam images.", ephemeral=True)

        lines = []
        for scam_hash in hashes:
            line = (
                f"`#{scam_hash.id}` · {scam_hash.source} · hits {scam_hash.hits} · "
                f"added {discord.utils.format_dt(scam_hash.created_at, 'R')}"
            )
            lines.append(line)

        embed = discord.Embed(
            title="Known scam images",
            description="\n".join(lines)[:4096],
            color=discord.Color.red(),
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @scamimage_group.command(name="remove", description="Remove a scam image hash by ID")
    @discord.option("id", int, description="Scam image hash ID", min_value=1)
    async def scamimage_remove(self, ctx: discord.ApplicationContext, id: int):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        removed = await scam_images.remove(id)
        if not removed:
            return await ctx.respond(f"Scam image #{id} doesn't exist or was already removed.", ephemeral=True)
        await ctx.respond(f"Removed scam image #{id}.", ephemeral=True)

    # --- tempban expiry ----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.expire_tempbans.is_running():
            self.expire_tempbans.start()

    def cog_unload(self):
        self.expire_tempbans.cancel()

    @tasks.loop(minutes=1)
    async def expire_tempbans(self):
        try:
            due = await mod_cases.due_expirations(discord.utils.utcnow())
        except Exception:
            log.exception("Couldn't load due tempbans")
            return
        for case in due:
            guild = self.bot.get_guild(case.guild_id)
            if guild is None or case.action != "ban":
                continue
            try:
                try:
                    await mod_actions.perform(
                        self.bot,
                        guild,
                        action="unban",
                        target_id=case.user_id,
                        moderator=None,
                        reason=f"Tempban expired (case #{case.id})",
                        source="tempban",
                        notify=False,
                    )
                except ModActionError as error:
                    if error.status != 404:
                        raise
                    await mod_cases.deactivate_case(case.guild_id, case.id)  # already unbanned by hand
            except Exception:
                log.warning("Couldn't lift tempban case #%s", case.id, exc_info=True)

    @expire_tempbans.before_loop
    async def _before_expire_tempbans(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(ModCommandsCog(bot))
