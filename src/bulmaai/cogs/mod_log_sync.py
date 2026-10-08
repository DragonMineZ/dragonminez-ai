"""Records moderation actions we didn't perform ourselves into mod_cases.

Two sources, both best-effort (a failed insert logs and moves on):
- Discord's audit log (bans, kicks, unbans, timeouts done via Discord's UI or by other bots).
- Dyno's mod-log channel embeds (adds the real moderator behind Dyno's own audit log entries).
Panel actions and automod hits are already recorded where they happen (mod_cases.py callers).
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import discord
from discord.ext import commands

from bulmaai.config import Settings
from bulmaai.services import mod_actions, mod_cases
from bulmaai.services.mod_actions import parse_duration_seconds
from bulmaai.utils.lifecycle import ReloadableCog

log = logging.getLogger(__name__)

_BACKFILL_AUDIT_LIMIT = 500
_BACKFILL_MESSAGE_LIMIT = 500

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
    dyno_user_id: int,
    dyno_modlog_configured: bool,
) -> MappedCase | None:
    """Pure mapping from a Discord audit log entry to a mod_cases row. None means skip."""
    executor_id = _entry_user_id(entry)
    if executor_id is not None and executor_id == bot_user_id:
        return None  # our own panel/automod actions are already recorded directly

    is_dyno = executor_id == dyno_user_id
    if is_dyno and dyno_modlog_configured:
        return None  # the Dyno mod-log parser records these with the real moderator

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
        source="dyno" if is_dyno else "discord",
        external_id=f"audit:{entry.id}",
        created_at=entry.created_at,
    )


# --- Dyno mod-log embed parsing -------------------------------------------------

_MENTION_RE = re.compile(r"<@!?(\d{15,20})>")
_NAME_ID_RE = re.compile(r"\((\d{15,20})\)")
_FOOTER_ID_RE = re.compile(r"(\d{15,20})")
_CASE_NUMBER_RE = re.compile(r"case\s*#?\s*(\d+)", re.IGNORECASE)
_NO_REASON = {"no reason given", "none", "n/a", ""}

# Dyno's own labels normalized to our action vocabulary (untouched ones pass through lowercased).
_ACTION_ALIASES = {
    "ban": "ban",
    "unban": "unban",
    "softban": "softban",
    "kick": "kick",
    "mute": "timeout",
    "unmute": "untimeout",
    "timeout": "timeout",
    "untimeout": "untimeout",
    "warn": "warn",
    "note": "note",
    "purge": "purge",
}


@dataclass(frozen=True, slots=True)
class ParsedCase:
    case_number: int | None
    action: str
    user_id: int
    moderator_id: int | None
    reason: str | None
    duration_seconds: int | None


def _extract_user_id(value: str | None) -> int | None:
    if not value:
        return None
    match = _MENTION_RE.search(value) or _NAME_ID_RE.search(value)
    if match:
        return int(match.group(1))
    stripped = value.strip()
    return int(stripped) if stripped.isdigit() else None


def _field_value(embed: dict, *names: str) -> str | None:
    for field in embed.get("fields") or []:
        if (field.get("name") or "").strip().lower() in names:
            return field.get("value")
    return None


def _parse_action(header: str) -> str | None:
    for part in header.split("|"):
        word = _CASE_NUMBER_RE.sub("", part).strip().lower()
        normalized = _ACTION_ALIASES.get(word)
        if normalized:
            return normalized
    lowered = header.lower()
    for alias, normalized in _ACTION_ALIASES.items():
        if re.search(rf"\b{alias}\b", lowered):
            return normalized
    return None


def parse_dyno_case(embed: dict) -> ParsedCase | None:
    """Tolerant parser for Dyno's mod-log case embed. Never raises; None means unparseable."""
    try:
        return _parse_dyno_case(embed)
    except Exception:
        log.debug("Failed to parse Dyno mod-log embed", exc_info=True)
        return None


def _parse_dyno_case(embed: dict) -> ParsedCase | None:
    header = embed.get("title") or (embed.get("author") or {}).get("name") or ""
    if not header:
        return None

    case_match = _CASE_NUMBER_RE.search(header)
    case_number = int(case_match.group(1)) if case_match else None

    action = _parse_action(header)
    if action is None:
        return None

    user_id = _extract_user_id(_field_value(embed, "user", "member"))
    if user_id is None:
        footer_text = (embed.get("footer") or {}).get("text") or ""
        footer_match = _FOOTER_ID_RE.search(footer_text)
        user_id = int(footer_match.group(1)) if footer_match else None
    if user_id is None:
        return None

    moderator_id = _extract_user_id(_field_value(embed, "moderator", "mod"))

    reason = _field_value(embed, "reason")
    if reason is not None:
        reason = reason.strip()
        if reason.lower() in _NO_REASON:
            reason = None

    duration_seconds = parse_duration_seconds(_field_value(embed, "length", "duration"))

    return ParsedCase(
        case_number=case_number,
        action=action,
        user_id=user_id,
        moderator_id=moderator_id,
        reason=reason,
        duration_seconds=duration_seconds,
    )


class ModLogSyncCog(ReloadableCog):
    """Listens for moderation done outside the bot (Discord's UI, other bots, Dyno)."""

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
        mapped = map_audit_entry(
            entry,
            bot_user_id=getattr(self.bot.user, "id", None),
            dyno_user_id=settings.dyno_user_id,
            dyno_modlog_configured=settings.dyno_modlog_channel_id is not None,
        )
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
        if mapped.source == "discord":  # Dyno posts its own; ours come from perform()
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        settings = self._settings()
        if message.guild is None or message.guild.id != settings.panel_guild_id:
            return
        if settings.dyno_modlog_channel_id is None or message.channel.id != settings.dyno_modlog_channel_id:
            return
        if message.author.id != settings.dyno_user_id:
            return
        for embed in message.embeds:
            await self._record_dyno_embed(
                embed.to_dict(),
                guild_id=settings.panel_guild_id,
                message_id=message.id,
                created_at=message.created_at,
                dyno_user_id=settings.dyno_user_id,
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

    async def _record_dyno_embed(
        self,
        embed: dict,
        *,
        guild_id: int,
        message_id: int,
        created_at: datetime,
        dyno_user_id: int,
    ) -> None:
        parsed = parse_dyno_case(embed)
        if parsed is None:
            log.debug(
                "Could not parse Dyno mod-log embed",
                extra={"event": "mod_log_sync_dyno_parse_skipped", "message_id": message_id},
            )
            return
        suffix = f":{parsed.case_number}" if parsed.case_number is not None else ""
        external_id = f"dyno:{message_id}{suffix}"
        try:
            await mod_cases.record_case(
                guild_id=guild_id,
                user_id=parsed.user_id,
                action=parsed.action,
                moderator_id=parsed.moderator_id or dyno_user_id,
                reason=parsed.reason,
                duration_seconds=parsed.duration_seconds,
                source="dyno",
                external_id=external_id,
                created_at=created_at,
            )
        except Exception:
            log.exception(
                "Failed to record Dyno mod-log case",
                extra={"event": "mod_log_sync_dyno_record_failed", "external_id": external_id},
            )

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
        await self._backfill_audit_log(guild, settings)
        await self._backfill_dyno_modlog(settings)

    async def _backfill_audit_log(self, guild: discord.Guild, settings: Settings) -> None:
        bot_user_id = getattr(self.bot.user, "id", None)
        dyno_modlog_configured = settings.dyno_modlog_channel_id is not None
        try:
            async for entry in guild.audit_logs(limit=_BACKFILL_AUDIT_LIMIT):
                mapped = map_audit_entry(
                    entry,
                    bot_user_id=bot_user_id,
                    dyno_user_id=settings.dyno_user_id,
                    dyno_modlog_configured=dyno_modlog_configured,
                )
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

    async def _backfill_dyno_modlog(self, settings: Settings, *, limit: int | None = _BACKFILL_MESSAGE_LIMIT) -> None:
        """limit=None walks the whole channel (one-off full Dyno history import); re-runs are idempotent."""
        channel_id = settings.dyno_modlog_channel_id
        if channel_id is None:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.Forbidden:
                log.warning(
                    "Missing access to the Dyno mod-log channel; skipped backfill",
                    extra={"event": "mod_log_sync_dyno_backfill_forbidden", "channel_id": channel_id},
                )
                return
            except Exception:
                log.exception(
                    "Failed to fetch the Dyno mod-log channel",
                    extra={"event": "mod_log_sync_dyno_backfill_fetch_failed", "channel_id": channel_id},
                )
                return
        try:
            async for message in channel.history(limit=limit):
                if message.author.id != settings.dyno_user_id:
                    continue
                for embed in message.embeds:
                    await self._record_dyno_embed(
                        embed.to_dict(),
                        guild_id=settings.panel_guild_id,
                        message_id=message.id,
                        created_at=message.created_at,
                        dyno_user_id=settings.dyno_user_id,
                    )
        except discord.Forbidden:
            log.warning(
                "Missing Read Message History in the Dyno mod-log channel; skipped backfill",
                extra={"event": "mod_log_sync_dyno_backfill_history_forbidden", "channel_id": channel_id},
            )
        except Exception:
            log.exception(
                "Dyno mod-log backfill failed",
                extra={"event": "mod_log_sync_dyno_backfill_failed", "channel_id": channel_id},
            )


def setup(bot: discord.Bot):
    bot.add_cog(ModLogSyncCog(bot))
