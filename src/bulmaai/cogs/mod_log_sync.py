"""Records moderation actions we didn't perform ourselves into mod_cases.

Source is Discord's audit log (bans, kicks, unbans, timeouts done via Discord's UI or by other bots),
best-effort (a failed insert logs and moves on); each recorded case also gets our case card.
Panel actions and automod hits are already recorded where they happen (mod_cases.py callers).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import discord
from discord.ext import commands

from bulmaai.config import Settings
from bulmaai.services import mod_actions, mod_cases
from bulmaai.utils.lifecycle import ReloadableCog

log = logging.getLogger(__name__)

_BACKFILL_AUDIT_LIMIT = 500

_AUDIT_ACTION_MAP: dict[discord.AuditLogAction, str] = {
    discord.AuditLogAction.ban: "ban",
    discord.AuditLogAction.unban: "unban",
    discord.AuditLogAction.kick: "kick",
}

_ENDS = {"untimeout": "timeout", "unban": "ban"}  # synced action -> the case action it ends

_MISSING = object()


@dataclass(frozen=True, slots=True)
class MappedCase:
    action: str
    user_id: int
    moderator_id: int | None
    reason: str | None
    duration_seconds: int | None
    source: str
    external_id: str
    created_at: datetime


def _entry_target_id(entry: discord.AuditLogEntry) -> int | None:
    # entry.target resolves through the member cache and is often None right after a
    # ban/kick (the target was just removed from it); the raw id is more reliable.
    target_id = getattr(entry, "_target_id", None)
    if target_id is not None:
        return int(target_id)
    return getattr(entry.target, "id", None)


def _entry_user_id(entry: discord.AuditLogEntry) -> int | None:
    return getattr(entry.user, "id", None)


def _map_member_update(entry: discord.AuditLogEntry) -> tuple[str | None, int | None]:
    """member_update covers nick/roles/timeout/etc.; only report timeout changes."""
    after = getattr(entry.after, "communication_disabled_until", _MISSING)
    if after is _MISSING:
        return None, None
    if after is None:
        return "untimeout", None
    duration = max(int((after - entry.created_at).total_seconds()), 0)
    return "timeout", duration


def map_audit_entry(
    entry: discord.AuditLogEntry,
    *,
    bot_user_id: int | None,
) -> MappedCase | None:
    """Pure mapping from a Discord audit log entry to a mod_cases row. None means skip."""
    executor_id = _entry_user_id(entry)
    if executor_id is not None and executor_id == bot_user_id:
        return None  # our own panel/automod actions are already recorded directly

    duration_seconds: int | None = None
    if entry.action is discord.AuditLogAction.member_update:
        action, duration_seconds = _map_member_update(entry)
    else:
        # member_prune has no single target user; anything else is out of scope.
        action = _AUDIT_ACTION_MAP.get(entry.action)
    if action is None:
        return None

    target_id = _entry_target_id(entry)
    if target_id is None:
        return None

    return MappedCase(
        action=action,
        user_id=target_id,
        moderator_id=executor_id,
        reason=entry.reason,
        duration_seconds=duration_seconds,
        source="discord",
        external_id=f"audit:{entry.id}",
        created_at=entry.created_at,
    )


class ModLogSyncCog(ReloadableCog):
    """Listens for moderation done outside the bot (Discord's UI, other bots)."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._backfill_task: asyncio.Task | None = None

    def _settings(self) -> Settings:
        return self.bot.settings

    # --- live listeners ----------------------------------------------------

    @commands.Cog.listener()
    async def on_audit_log_entry(self, entry: discord.AuditLogEntry) -> None:
        settings = self._settings()
        if entry.guild.id != settings.panel_guild_id:
            return
        mapped = map_audit_entry(entry, bot_user_id=getattr(self.bot.user, "id", None))
        if mapped is None:
            return
        case_id = await self._record_mapped_case(mapped, guild_id=settings.panel_guild_id)
        if case_id is None:
            return
        ended = _ENDS.get(mapped.action)
        if ended:  # the matching mute/ban card flips to ended, credited to the moderator from the audit log
            try:
                await mod_actions.end_user_cases(
                    self.bot, settings.panel_guild_id, mapped.user_id, ended, ended_by=mapped.moderator_id
                )
            except Exception:
                log.exception("Couldn't end the %s cases of %s", ended, mapped.user_id)
        await mod_actions.post_case_log(
            self.bot,
            case_id=case_id,
            action=mapped.action,
            user_id=mapped.user_id,
            moderator_id=mapped.moderator_id,
            reason=mapped.reason,
            duration_seconds=mapped.duration_seconds,
            source="discord",
            guild_id=settings.panel_guild_id,
        )

    # --- recording -----------------------------------------------------------

    async def _record_mapped_case(self, mapped: MappedCase, *, guild_id: int) -> int | None:
        try:
            return await mod_cases.record_case(
                guild_id=guild_id,
                user_id=mapped.user_id,
                action=mapped.action,
                moderator_id=mapped.moderator_id,
                reason=mapped.reason,
                duration_seconds=mapped.duration_seconds,
                source=mapped.source,
                external_id=mapped.external_id,
                created_at=mapped.created_at,
            )
        except Exception:
            log.exception(
                "Failed to record audit log case",
                extra={"event": "mod_log_sync_audit_record_failed", "external_id": mapped.external_id},
            )
            return None

    # --- backfill --------------------------------------------------------------

    async def on_startup(self) -> None:
        # Idempotent, so a reload re-running it only catches up on what happened meanwhile.
        self._backfill_task = asyncio.create_task(self._backfill())

    async def on_shutdown(self) -> None:
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            self._backfill_task = None

    async def _backfill(self) -> None:
        settings = self._settings()
        guild = self.bot.get_guild(settings.panel_guild_id)
        if guild is None:
            return
        await self._backfill_audit_log(guild)

    async def _backfill_audit_log(self, guild: discord.Guild) -> None:
        bot_user_id = getattr(self.bot.user, "id", None)
        try:
            async for entry in guild.audit_logs(limit=_BACKFILL_AUDIT_LIMIT):
                mapped = map_audit_entry(entry, bot_user_id=bot_user_id)
                if mapped is not None:
                    await self._record_mapped_case(mapped, guild_id=guild.id)
        except discord.Forbidden:
            log.warning(
                "Missing View Audit Log permission; skipped audit log backfill",
                extra={"event": "mod_log_sync_audit_backfill_forbidden", "guild_id": guild.id},
            )
        except Exception:
            log.exception(
                "Audit log backfill failed",
                extra={"event": "mod_log_sync_audit_backfill_failed", "guild_id": guild.id},
            )


def setup(bot: discord.Bot):
    bot.add_cog(ModLogSyncCog(bot))
