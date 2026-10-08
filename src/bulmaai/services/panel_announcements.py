"""Shared building blocks for panel-composed announcements.

Turns a JSON payload ({card, mention_roles, mention_everyone, translate}, as saved in the
panel_announcements table) into a Components V2 view plus allowed mentions, translates cards, and does
the claim-and-send used by both the scheduled-announcements cog and the panel's "send now" route.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from bulmaai.cogs.ai_ann_translation import swap_role_mentions, translate_text, translated_role_mentions
from bulmaai.database.db import get_pool
from bulmaai.services.cards import AnnouncementError, build_card_view, normalize_card

log = logging.getLogger(__name__)

OVERDUE_AFTER = timedelta(hours=6)
OLD_EDITOR_MESSAGE = "This announcement was made with the old editor; recreate it."

LANGUAGE_CHANNEL_SETTINGS = {"es": "announcement_spanish_channel_id", "pt": "announcement_portuguese_channel_id"}


def build_allowed_mentions(guild: discord.Guild, mention_role_ids: Any, mention_everyone: Any) -> discord.AllowedMentions:
    mention_role_ids = mention_role_ids or []
    if not isinstance(mention_role_ids, list):
        raise AnnouncementError("mention_roles must be a list.")
    roles = []
    for raw_id in mention_role_ids:
        try:
            role_id = int(raw_id)
        except (TypeError, ValueError):
            raise AnnouncementError("mention_roles must be role IDs.")
        if guild.get_role(role_id) is None:
            raise AnnouncementError(f"Role {raw_id} isn't in this server.")
        roles.append(discord.Object(id=role_id))
    mention_everyone = False if mention_everyone is None else mention_everyone
    if not isinstance(mention_everyone, bool):
        raise AnnouncementError("mention_everyone must be true or false.")
    return discord.AllowedMentions(roles=roles, users=False, everyone=mention_everyone)


@dataclass
class BuiltMessage:
    view: discord.ui.DesignerView
    mentions: discord.AllowedMentions


def build_message(guild: discord.Guild, payload: dict[str, Any]) -> BuiltMessage:
    if not isinstance(payload.get("card"), dict):
        raise AnnouncementError(OLD_EDITOR_MESSAGE)
    view = build_card_view(normalize_card(payload["card"]))
    mentions = build_allowed_mentions(guild, payload.get("mention_roles"), payload.get("mention_everyone", False))
    return BuiltMessage(view=view, mentions=mentions)


# ---------- channel / permission checks ----------


def resolve_channel(guild: discord.Guild, channel_id: Any) -> Any:
    try:
        channel = guild.get_channel(int(channel_id))
    except (TypeError, ValueError):
        raise AnnouncementError("Pick a channel.")
    if channel is None or str(channel.type) not in {"text", "news"}:
        raise AnnouncementError("Pick a text or announcement channel in this server.")
    return channel


def check_bot_can(guild: discord.Guild, channel: Any, *, edit: bool) -> None:
    perms = channel.permissions_for(guild.me)
    needed = [("View Channel", perms.view_channel)]
    needed.append(("Read Message History", perms.read_message_history) if edit else ("Send Messages", perms.send_messages))
    missing = [name for name, ok in needed if not ok]
    if missing:
        raise AnnouncementError(f"The bot is missing {', '.join(missing)} in #{channel.name}.")


# ---------- sending ----------


async def send_built_message(channel: Any, built: BuiltMessage) -> list[str]:
    try:
        message = await channel.send(view=built.view, allowed_mentions=built.mentions)
    except discord.HTTPException as error:
        raise AnnouncementError(f"Discord rejected the message: {error.text or error}")
    return [str(message.id)]


async def translate_card(cog: Any, card: dict[str, Any], lang: str, *, swap_mentions: bool = True) -> dict[str, Any]:
    """A translated copy of the card: every text/section text, gallery description and button label."""
    out = deepcopy(card)
    for block in out["blocks"]:
        if block["type"] in ("text", "section"):
            text = await translate_text(cog, block["text"], lang)
            block["text"] = swap_role_mentions(text, lang, cog) if swap_mentions else text
        elif block["type"] == "gallery":
            for image in block["images"]:
                if image["description"]:
                    image["description"] = await translate_text(cog, image["description"], lang)
        elif block["type"] == "buttons":
            for button in block["buttons"]:
                button["label"] = await translate_text(cog, button["label"], lang)
    return out


async def _send_one_translation(bot: Any, guild: discord.Guild, channel: Any, payload: dict[str, Any], lang: str) -> None:
    cog = bot.get_cog("AiAnnTranslation")
    if cog is None:
        raise AnnouncementError("Translation isn't available right now (AiAnnTranslation cog not loaded).")
    translated = await translate_card(cog, normalize_card(payload["card"]), lang)
    view = build_card_view(normalize_card(translated))
    target_channel_id = getattr(bot.settings, LANGUAGE_CHANNEL_SETTINGS[lang], None)
    target = resolve_channel(guild, target_channel_id) if target_channel_id else channel
    check_bot_can(guild, target, edit=False)
    base = build_allowed_mentions(guild, payload.get("mention_roles"), payload.get("mention_everyone", False))
    mentions = translated_role_mentions(
        [int(role_id) for role_id in payload.get("mention_roles") or []], lang, cog.settings, everyone=base.everyone
    )
    await send_built_message(target, BuiltMessage(view, mentions))


async def send_translations(bot: Any, guild: discord.Guild, channel: Any, payload: dict[str, Any]) -> dict[str, str]:
    """Best-effort: a failed translation is reported here but never blocks/undoes the main send."""
    wanted = payload.get("translate") or {}
    errors: dict[str, str] = {}
    for lang, on in wanted.items():
        if not on or lang not in LANGUAGE_CHANNEL_SETTINGS:
            continue
        try:
            await _send_one_translation(bot, guild, channel, payload, lang)
        except Exception as error:
            if not isinstance(error, AnnouncementError):
                log.exception("Failed to send %s translation of a panel announcement", lang)
            errors[lang] = str(error)
    return errors


# ---------- scheduling: claim, mark, and the shared send-a-due-row path ----------


async def claim_scheduled(pool, row_id: int):
    return await pool.fetchrow(
        "UPDATE panel_announcements SET status = 'sending', updated_at = now() "
        "WHERE id = $1 AND status = 'scheduled' "
        "RETURNING id, author_id, channel_id, payload, send_at",
        row_id,
    )


def _load_payload(row) -> dict[str, Any]:
    payload = row["payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


async def mark_sent(pool, row_id: int, message_ids: dict[str, list[str]], translation_errors: dict[str, str] | None) -> None:
    if translation_errors:
        await pool.execute(
            "UPDATE panel_announcements SET status = 'sent', message_ids = $2::jsonb, sent_at = now(), "
            "updated_at = now(), payload = payload || $3::jsonb WHERE id = $1",
            row_id,
            json.dumps(message_ids),
            json.dumps({"translation_errors": translation_errors}),
        )
    else:
        await pool.execute(
            "UPDATE panel_announcements SET status = 'sent', message_ids = $2::jsonb, sent_at = now(), "
            "updated_at = now() WHERE id = $1",
            row_id,
            json.dumps(message_ids),
        )


async def mark_failed(pool, row_id: int, error: str) -> None:
    await pool.execute(
        "UPDATE panel_announcements SET status = 'failed', error = $2, updated_at = now() WHERE id = $1",
        row_id,
        error[:2000],
    )


async def mark_overdue(pool, row_id: int, send_at: datetime) -> bool:
    """Directly fails a scheduled row that's overdue by more than OVERDUE_AFTER, without ever
    sending it. Returns False if it was claimed/cancelled/edited out from under us first."""
    hours = int((datetime.now(timezone.utc) - send_at).total_seconds() // 3600)
    result = await pool.execute(
        "UPDATE panel_announcements SET status = 'failed', "
        "error = $2, updated_at = now() WHERE id = $1 AND status = 'scheduled'",
        row_id,
        f"Skipped: overdue by more than {hours}h when the bot checked it.",
    )
    return result != "UPDATE 0"


async def send_scheduled(bot: Any, row_id: int) -> dict[str, Any]:
    """Claims one due row and sends it. Used by the scheduler loop's regular tick and by the
    panel's "send now" action. Never raises; the row always ends up sent or failed (with why)."""
    pool = await get_pool()
    row = await claim_scheduled(pool, row_id)
    if row is None:
        return {"id": row_id, "status": "failed", "error": "Already sent, cancelled, or in progress."}
    guild = bot.get_guild(bot.settings.panel_guild_id)
    payload = _load_payload(row)
    try:
        if guild is None:
            raise AnnouncementError("Bot is not connected to the guild.")
        channel = resolve_channel(guild, row["channel_id"])
        built = build_message(guild, payload)
        check_bot_can(guild, channel, edit=False)
        message_ids = await send_built_message(channel, built)
        translation_errors = await send_translations(bot, guild, channel, payload)
        await mark_sent(pool, row["id"], {"main": message_ids}, translation_errors or None)
        return {"id": row["id"], "status": "sent", "message_ids": message_ids, "translation_errors": translation_errors}
    except Exception as error:
        message = str(error) if isinstance(error, AnnouncementError) else f"Unexpected error: {error}"
        if not isinstance(error, AnnouncementError):
            log.exception("Failed to send scheduled announcement %s", row["id"])
        await mark_failed(pool, row["id"], message)
        return {"id": row["id"], "status": "failed", "error": message}
