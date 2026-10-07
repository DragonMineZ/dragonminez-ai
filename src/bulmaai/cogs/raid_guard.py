"""Anti-raid and join gate: replaces Dyno's raid mode. Tracks the join rate, opens raid mode when
joins spike (applying an action to each joiner while it lasts), and outside raid mode flags new
accounts and returning offenders.
ponytail: raid mode itself is in-memory only — a restart silently ends it, an acceptable trade for
staying simple. Flagged-joiner alerts are different: they're logged to joiner_alerts (services/joiner_alerts.py)
the moment they're posted (on the flagged user's first message, not on join), and a tasks.loop sweep (not an in-memory timer) auto-dismisses an alert 1h after
it's posted if no staff quick-action click resolved it first — so the deadline survives a restart."""

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from bulmaai.services import joiner_alerts, mod_actions, mod_cases
from bulmaai.ui.mod_views import RAID, parse_custom_id, quick_actions_view, raid_view
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.web.core import PERMISSIONS, tier_for

log = logging.getLogger(__name__)

RAID_TIMEOUT_SECONDS = 3600  # ponytail: 1h timeout for joiners caught during raid mode
NEW_ACCOUNT_TIMEOUT_SECONDS = 86400  # ponytail: 1 day timeout for new-account joiners
RAID_ALERT_DEBOUNCE_SECONDS = 5
RAID_JOINER_LOG_CAP = 25  # the alert embed only ever shows the most recent N joiners
OFFENSE_ACTIONS = ("warn", "timeout", "kick", "ban")
JOINER_ALERT_TIMEOUT_SECONDS = 3600  # auto-dismiss an untouched flagged-joiner alert after 1h


# --- pure helpers (unit tested directly) --------------------------------------------------------


def is_raid(join_times, *, now: float, count: int, window_seconds: int) -> bool:
    """True once `count` of `join_times` (monotonic seconds) fall within `window_seconds` of `now`."""
    cutoff = now - window_seconds
    return sum(1 for joined in join_times if joined >= cutoff) >= count


def is_new_account(created_at: datetime, *, now: datetime, days: int) -> bool:
    """True when the account is younger than `days` days old. days<=0 turns the check off, and an
    account exactly `days` old is not flagged (strictly younger than)."""
    if days <= 0:
        return False
    return (now - created_at) < timedelta(days=days)


def offense_breakdown(cases) -> dict[str, int]:
    """{action: count} for active warns/timeouts/kicks/bans among the given mod_cases.ModCase rows."""
    counts: dict[str, int] = {}
    for case in cases:
        if case.active and case.action in OFFENSE_ACTIONS:
            counts[case.action] = counts.get(case.action, 0) + 1
    return counts


class RaidGuardCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._join_times: deque[float] = deque()
        self._raid_until: float | None = None
        self._raid_message: discord.Message | None = None
        self._raid_joiners: list[tuple[int, str, str]] = []  # (user_id, display_name, action_taken)
        # ponytail: flagged joiners wait here for their first message, in-memory (a restart drops them), DB-backed if that ever matters
        self._pending_joiners: dict[int, tuple[bool, dict[str, int], str]] = {}
        self._raid_revision = 0
        self._raid_update_task: asyncio.Task | None = None
        self._debounce_seconds = RAID_ALERT_DEBOUNCE_SECONDS  # tests set this to 0

    def _settings(self):
        return self.bot.settings

    def _in_raid(self, now: float) -> bool:
        return self._raid_until is not None and self._raid_until > now

    def _active_in(self, guild: discord.Guild | None) -> bool:
        settings = self._settings()
        return bool(guild) and guild.id == settings.panel_guild_id and settings.moderation_enabled

    def _actor_allowed(self, member) -> bool:
        # Duck-typed like utils.permissions.is_admin: a DM/user-install interaction hands us a
        # discord.User with no guild_permissions, which is never allowed here.
        if getattr(member, "guild_permissions", None) is None:
            return False
        return tier_for(member, self._settings()) >= PERMISSIONS["mod.channels"]

    def _end_raid_mode(self) -> None:
        self._raid_until = None
        self._join_times.clear()
        self._raid_joiners = []
        self._raid_message = None

    # --- join handling -------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not self._active_in(member.guild):
            return
        await self._handle_join(member)

    async def _handle_join(self, member: discord.Member) -> None:
        settings = self._settings()
        now = time.monotonic()
        was_raid = self._in_raid(now)
        self._join_times.append(now)
        cutoff = now - settings.moderation_raid_join_window_seconds
        while self._join_times and self._join_times[0] < cutoff:
            self._join_times.popleft()

        entering = not was_raid and is_raid(
            self._join_times,
            now=now,
            count=settings.moderation_raid_join_count,
            window_seconds=settings.moderation_raid_join_window_seconds,
        )
        if entering or was_raid:
            self._raid_until = now + settings.moderation_raid_mode_minutes * 60
            if entering:
                self._raid_joiners = []  # a new raid after the last one expired starts a fresh list
            action = await self._apply_join_action(
                member, settings.moderation_raid_action, duration_seconds=RAID_TIMEOUT_SECONDS, reason="Joined during a raid"
            )
            self._record_raid_joiner(member, action)
            if entering:
                await self._post_raid_alert()
            else:
                self._schedule_raid_update()
            return

        await self._check_new_joiner(member, settings)

    async def _apply_join_action(self, member: discord.Member, action: str, *, duration_seconds: int, reason: str) -> str:
        if action not in ("timeout", "kick"):
            return "alert"
        try:
            await mod_actions.perform(
                self.bot,
                member.guild,
                action=action,
                target_id=member.id,
                moderator=None,
                duration_seconds=duration_seconds,  # perform() ignores it for kicks
                reason=reason,
                source="antiraid",
            )
            return action
        except mod_actions.ModActionError as error:
            log.warning(
                "Join action %s failed for %s: %s",
                action,
                member.id,
                error,
                extra={"event": "raid_guard_action_failed", "user_id": member.id, "action": action},
            )
            return "failed"

    def _record_raid_joiner(self, member: discord.Member, action: str) -> None:
        self._raid_joiners.append((member.id, str(member), action))
        if len(self._raid_joiners) > RAID_JOINER_LOG_CAP:
            self._raid_joiners = self._raid_joiners[-RAID_JOINER_LOG_CAP:]

    # --- outside raid mode: new accounts + returning offenders ----------------------------------

    async def _check_new_joiner(self, member: discord.Member, settings) -> None:
        now = discord.utils.utcnow()
        is_new = is_new_account(member.created_at, now=now, days=settings.moderation_new_account_days)

        breakdown: dict[str, int] = {}
        try:
            cases = await mod_cases.list_cases(member.guild.id, user_id=member.id, limit=50)
            breakdown = offense_breakdown(cases)
        except Exception:
            log.exception(
                "Failed to check case history for new joiner",
                extra={"event": "raid_guard_history_check_failed", "user_id": member.id},
            )

        if not is_new and not breakdown:
            return

        action_taken = "alert"
        if is_new:
            action_taken = await self._apply_join_action(
                member,
                settings.moderation_new_account_action,
                duration_seconds=NEW_ACCOUNT_TIMEOUT_SECONDS,
                reason="New Discord account joined",
            )

        self._pending_joiners[member.id] = (is_new, breakdown, action_taken)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.author.id not in self._pending_joiners or not self._active_in(message.guild):
            return
        is_new, breakdown, action_taken = self._pending_joiners.pop(message.author.id)
        await self._post_joiner_alert(message.author, is_new=is_new, breakdown=breakdown, action_taken=action_taken)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        self._pending_joiners.pop(member.id, None)

    async def _post_joiner_alert(self, member: discord.Member, *, is_new: bool, breakdown: dict[str, int], action_taken: str) -> None:
        channel = await mod_actions.resolve_channel(self.bot, mod_actions.mod_log_channel_id(self._settings()))
        if channel is None:
            return
        embed = discord.Embed(title="Flagged Joiner", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        if is_new:
            embed.add_field(name="Flag", value="New account", inline=True)
            embed.add_field(name="Action taken", value=action_taken, inline=True)
        if breakdown:
            lines = ", ".join(f"{count}x {action}" for action, count in breakdown.items())
            embed.add_field(name="Case history", value=lines, inline=False)
        try:
            alert = await channel.send(
                embed=embed,
                view=quick_actions_view(member.id, actions=("kick", "ban", "dismiss")),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Failed to post flagged-joiner alert", extra={"event": "raid_guard_joiner_alert_send_failed"})
            return
        try:
            await joiner_alerts.record(
                guild_id=member.guild.id,
                user_id=member.id,
                reason="new_account" if is_new else "returning_offender",
                action_taken=action_taken,
                alert_message_id=alert.id,
                expires_at=discord.utils.utcnow() + timedelta(seconds=JOINER_ALERT_TIMEOUT_SECONDS),
            )
        except Exception:
            # Best-effort: the alert is still posted, it just won't be swept if no one clicks it.
            log.exception("Couldn't record a flagged-joiner alert", extra={"event": "raid_guard_joiner_alert_record_failed"})

    # --- 1h auto-dismiss sweep (durable: reads joiner_alerts, survives a restart) ---------------

    async def on_startup(self) -> None:
        if not self.expire_joiner_alerts.is_running():
            self.expire_joiner_alerts.start()

    async def on_shutdown(self) -> None:
        self.expire_joiner_alerts.cancel()
        if self._raid_update_task is not None and not self._raid_update_task.done():
            self._raid_update_task.cancel()

    def export_state(self) -> dict | None:
        # Raid mode must survive a reload or a code push mid-raid would silently stop quarantining joiners.
        if not self._join_times and self._raid_until is None and not self._pending_joiners:
            return None
        return {
            "join_times": list(self._join_times),
            "raid_until": self._raid_until,
            "raid_message": self._raid_message,
            "raid_joiners": list(self._raid_joiners),
            "raid_revision": self._raid_revision,
            "pending_joiners": dict(self._pending_joiners),
        }

    def import_state(self, state: dict) -> None:
        self._join_times = deque(state.get("join_times", ()))
        self._raid_until = state.get("raid_until")
        self._raid_message = state.get("raid_message")
        self._raid_joiners = list(state.get("raid_joiners", ()))
        self._raid_revision = state.get("raid_revision", 0)
        self._pending_joiners = dict(state.get("pending_joiners", {}))
        if self._raid_message is not None and self._in_raid(time.monotonic()):
            # An embed edit the old instance had pending was cancelled with it.
            self._schedule_raid_update()

    @tasks.loop(minutes=1)
    async def expire_joiner_alerts(self) -> None:
        try:
            due = await joiner_alerts.due(discord.utils.utcnow())
        except Exception:
            log.exception("Couldn't load due joiner alerts", extra={"event": "raid_guard_joiner_alert_sweep_failed"})
            return
        for alert in due:
            await self._auto_dismiss_joiner_alert(alert)

    @expire_joiner_alerts.before_loop
    async def _before_expire_joiner_alerts(self) -> None:
        await self.bot.wait_until_ready()

    async def _auto_dismiss_joiner_alert(self, alert: joiner_alerts.JoinerAlert) -> None:
        """A moderator's quick-action click and this sweep both try to resolve the same row; set_outcome's
        first-review-wins guard means only one of them ever actually deletes the message."""
        if not await joiner_alerts.set_outcome(alert.id, joiner_alerts.AUTO_DISMISSED, None):
            return
        try:
            await mod_cases.record_case(
                guild_id=alert.guild_id,
                user_id=alert.user_id,
                action="note",
                moderator_id=None,
                reason="Flagged-joiner alert auto-dismissed: no staff response within 1h",
                source="antiraid",
            )
        except Exception:
            log.exception(
                "Couldn't record the auto-dismiss of a flagged-joiner alert",
                extra={"event": "raid_guard_joiner_alert_note_failed", "alert_id": alert.id},
            )
        if alert.alert_message_id is None:
            return
        channel = await mod_actions.resolve_channel(self.bot, mod_actions.mod_log_channel_id(self._settings()))
        if channel is None or not hasattr(channel, "get_partial_message"):
            return
        try:
            await channel.get_partial_message(alert.alert_message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning(
                "Couldn't delete an auto-dismissed flagged-joiner alert",
                exc_info=True,
                extra={"event": "raid_guard_joiner_alert_delete_failed", "message_id": alert.alert_message_id},
            )

    # --- the raid alert embed (one message per raid, debounced edits) ---------------------------

    def _build_raid_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Raid Mode Active",
            description=f"{len(self._raid_joiners)} member(s) joined during this raid.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        shown = self._raid_joiners[-RAID_JOINER_LOG_CAP:]
        lines = [f"<@{user_id}> ({name}) — {action}" for user_id, name, action in shown]
        embed.add_field(
            name=f"Recent joiners (showing {len(shown)} of {len(self._raid_joiners)})",
            value="\n".join(lines) or "none",
            inline=False,
        )
        return embed

    async def _post_raid_alert(self) -> None:
        channel = await mod_actions.resolve_channel(self.bot, mod_actions.mod_log_channel_id(self._settings()))
        if channel is None:
            log.warning("Raid mode entered but no mod-log channel is configured", extra={"event": "raid_guard_no_log_channel"})
            return
        try:
            self._raid_message = await channel.send(
                embed=self._build_raid_embed(),
                view=raid_view(locked=False),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Failed to post raid alert", extra={"event": "raid_guard_alert_send_failed"})

    def _schedule_raid_update(self) -> None:
        self._raid_revision += 1
        if self._raid_update_task is None or self._raid_update_task.done():
            self._raid_update_task = asyncio.create_task(self._flush_raid_update())

    async def _flush_raid_update(self) -> None:
        # Debounced like automod's incident log: a burst of joiners becomes a few edits, not one per joiner.
        synced = 0
        while synced != self._raid_revision:
            await asyncio.sleep(self._debounce_seconds)
            if self._raid_message is None:
                return
            synced = self._raid_revision
            try:
                await self._raid_message.edit(embed=self._build_raid_embed())
            except discord.HTTPException:
                log.debug("Failed to update raid alert", exc_info=True)
                return

    # --- raid alert buttons ----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        parsed = parse_custom_id((interaction.data or {}).get("custom_id"))
        if parsed is None or parsed[0] != RAID:
            return
        await self._handle_raid_button(interaction, parsed[1][0])

    async def _handle_raid_button(self, interaction: discord.Interaction, action: str) -> None:
        if not self._actor_allowed(interaction.user):
            await interaction.response.send_message("You need moderator permissions for that.", ephemeral=True)
            return

        settings = self._settings()
        if action == "lockdown":
            await interaction.response.defer()
            locked, failed = await mod_actions.lockdown(
                interaction.guild, settings, moderator_id=interaction.user.id, reason="Raid lockdown"
            )
            await interaction.message.edit(view=raid_view(locked=True))
            await interaction.followup.send(f"🔒 Raid lockdown: locked {locked} channel(s), {failed} failed.")
        elif action == "unlock":
            await interaction.response.defer()
            unlocked, failed = await mod_actions.end_lockdown(interaction.guild, reason="Raid lockdown lifted")
            await interaction.message.edit(view=raid_view(locked=False))
            await interaction.followup.send(f"🔓 Raid lockdown lifted: unlocked {unlocked} channel(s), {failed} failed.")
        elif action == "end":
            self._end_raid_mode()
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(f"🛡️ Raid mode ended by {interaction.user.mention}.", allowed_mentions=discord.AllowedMentions.none())

    # --- /raidmode -------------------------------------------------------------------------------

    raidmode = discord.SlashCommandGroup(
        "raidmode", "Anti-raid controls", default_member_permissions=discord.Permissions(moderate_members=True)
    )

    @raidmode.command(name="on", description="Manually enter raid mode")
    @discord.option("minutes", int, description="Minutes to stay in raid mode (defaults to the configured length)", min_value=1, max_value=1440, required=False)
    async def raidmode_on(self, ctx: discord.ApplicationContext, minutes: int = None) -> None:
        if not self._actor_allowed(ctx.author):
            await ctx.respond("You need moderator permissions for that.", ephemeral=True)
            return
        settings = self._settings()
        length = minutes or settings.moderation_raid_mode_minutes
        self._raid_until = time.monotonic() + length * 60
        await self._post_raid_alert()
        await ctx.respond(f"🚨 Raid mode is now on for {length} minute(s).")

    @raidmode.command(name="off", description="Manually end raid mode")
    async def raidmode_off(self, ctx: discord.ApplicationContext) -> None:
        if not self._actor_allowed(ctx.author):
            await ctx.respond("You need moderator permissions for that.", ephemeral=True)
            return
        self._end_raid_mode()
        await ctx.respond("🛡️ Raid mode turned off.")

    @raidmode.command(name="status", description="Show the current raid mode status")
    async def raidmode_status(self, ctx: discord.ApplicationContext) -> None:
        if not self._actor_allowed(ctx.author):
            await ctx.respond("You need moderator permissions for that.", ephemeral=True)
            return
        now = time.monotonic()
        if self._in_raid(now):
            remaining = int(self._raid_until - now)
            await ctx.respond(
                f"🚨 Raid mode is ON, {remaining}s remaining. {len(self._raid_joiners)} joiner(s) tracked this raid.",
            )
        else:
            await ctx.respond(f"🛡️ Raid mode is off. {len(self._join_times)} recent join(s) tracked.")


def setup(bot: discord.Bot):
    bot.add_cog(RaidGuardCog(bot))
