"""Shared building blocks for panel-composed announcements.

Turns a JSON payload (as saved in the panel_announcements table) into real Discord objects
(embeds, link buttons, allowed mentions), checks Discord's length limits, and does the
claim-and-send used by both the scheduled-announcements cog and the panel's "send now" route.
"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import discord

from bulmaai.cogs.ai_ann_translation import (
    build_announcement_sends,
    swap_role_mentions,
    translate_text,
    translated_role_mentions,
)
from bulmaai.database.db import get_pool

log = logging.getLogger(__name__)

MAX_EMBEDS = 10
MAX_FIELDS = 25
MAX_BUTTONS = 5
EMBED_TOTAL_LIMIT = 6000
OVERDUE_AFTER = timedelta(hours=6)
HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})")

LANGUAGE_CHANNEL_SETTINGS = {"es": "announcement_spanish_channel_id", "pt": "announcement_portuguese_channel_id"}


class AnnouncementError(Exception):
    """A payload problem safe to show the staff member who submitted it."""


# ---------- validation helpers ----------


def _limit(value: str | None, maximum: int, label: str) -> None:
    if value and len(value) > maximum:
        raise AnnouncementError(f"{label} is {len(value)} characters; Discord allows {maximum}.")


def _text(value: Any, label: str, *, optional: bool = False, strip: bool = True) -> str | None:
    if value is None or value == "":
        if optional:
            return None
        raise AnnouncementError(f"{label} can't be empty.")
    if not isinstance(value, str):
        raise AnnouncementError(f"{label} must be text.")
    if not value.strip():
        if optional:
            return None
        raise AnnouncementError(f"{label} can't be empty.")
    return value.strip() if strip else value


def url(value: str | None, label: str) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(value) > 2048:
        raise AnnouncementError(f"{label} must be an http(s) link.")
    return value


def _color(value: str | None, label: str) -> int | None:
    if not value:
        return None
    match = HEX_COLOR.fullmatch(value)
    if not match:
        raise AnnouncementError(f"{label} must be a hex code like #F39C12.")
    return int(match[1], 16)


def check_embed_limits(embeds: list[discord.Embed]) -> None:
    if len(embeds) > MAX_EMBEDS:
        raise AnnouncementError(f"That renders {len(embeds)} embeds; Discord allows {MAX_EMBEDS} per message.")
    for number, embed in enumerate(embeds, 1):
        where = f"Embed {number}" if len(embeds) > 1 else "Embed"
        raw = embed.to_dict()
        _limit(raw.get("title"), 256, f"{where} title")
        _limit(raw.get("description"), 4096, f"{where} description")
        _limit(raw.get("footer", {}).get("text"), 2048, f"{where} footer")
        _limit(raw.get("author", {}).get("name"), 256, f"{where} author")
        fields = raw.get("fields", [])
        if len(fields) > MAX_FIELDS:
            raise AnnouncementError(f"{where} has {len(fields)} fields; Discord allows {MAX_FIELDS}.")
        for field in fields:
            _limit(field["name"], 256, f"{where} field name “{field['name'][:40]}”")
            _limit(field["value"], 1024, f"{where} field “{field['name'][:40]}”")
    total = sum(len(embed) for embed in embeds)
    if total > EMBED_TOTAL_LIMIT:
        raise AnnouncementError(f"Embeds total {total} characters; Discord allows {EMBED_TOTAL_LIMIT} per message.")


def build_embed(data: dict[str, Any]) -> discord.Embed:
    if not isinstance(data, dict):
        raise AnnouncementError("Each embed must be an object.")
    fields = data.get("fields") or []
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise AnnouncementError(f"An embed allows at most {MAX_FIELDS} fields.")
    embed = discord.Embed(
        title=_text(data.get("title"), "Embed title", optional=True),
        description=_text(data.get("description"), "Embed description", optional=True, strip=False),
        url=url(data.get("url"), "Embed title link"),
        colour=_color(data.get("color"), "Embed color"),
        timestamp=datetime.now(timezone.utc) if data.get("timestamp") else None,
    )
    author_name = _text(data.get("author_name"), "Embed author", optional=True)
    if author_name:
        embed.set_author(
            name=author_name,
            url=url(data.get("author_url"), "Author link"),
            icon_url=url(data.get("author_icon_url"), "Author icon"),
        )
    for number, raw_field in enumerate(fields, 1):
        if not isinstance(raw_field, dict):
            raise AnnouncementError(f"Field {number} must be an object.")
        embed.add_field(
            name=_text(raw_field.get("name"), f"Field {number} name"),
            value=_text(raw_field.get("value"), f"Field {number} value", strip=False),
            inline=bool(raw_field.get("inline")),
        )
    image_url = url(data.get("image_url"), "Image URL")
    if image_url:
        embed.set_image(url=image_url)
    thumbnail_url = url(data.get("thumbnail_url"), "Thumbnail URL")
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    footer = _text(data.get("footer"), "Footer", optional=True)
    if footer:
        embed.set_footer(text=footer)
    return embed


def build_embeds(raw_embeds: Any) -> list[discord.Embed]:
    if not raw_embeds:
        return []
    if not isinstance(raw_embeds, list):
        raise AnnouncementError("embeds must be a list.")
    embeds = [build_embed(e) for e in raw_embeds]
    check_embed_limits(embeds)
    return embeds


def build_view(raw_buttons: Any) -> discord.ui.View | None:
    if not raw_buttons:
        return None
    if not isinstance(raw_buttons, list) or len(raw_buttons) > MAX_BUTTONS:
        raise AnnouncementError(f"A message allows at most {MAX_BUTTONS} link buttons.")
    view = discord.ui.View(timeout=None)
    for number, raw_button in enumerate(raw_buttons, 1):
        if not isinstance(raw_button, dict):
            raise AnnouncementError(f"Button {number} must be an object.")
        label = _text(raw_button.get("label"), f"Button {number} label")
        _limit(label, 80, f"Button {number} label")
        link = url(raw_button.get("url"), f"Button {number} URL")
        if not link:
            raise AnnouncementError(f"Button {number} needs a URL.")
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=link))
    return view


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
    if not isinstance(mention_everyone, bool):
        raise AnnouncementError("mention_everyone must be true or false.")
    return discord.AllowedMentions(roles=roles, users=False, everyone=mention_everyone)


@dataclass
class BuiltMessage:
    content: str | None
    embeds: list[discord.Embed]
    view: discord.ui.View | None
    mentions: discord.AllowedMentions


def build_message(guild: discord.Guild, payload: dict[str, Any]) -> BuiltMessage:
    content = _text(payload.get("content"), "Content", optional=True, strip=False)
    _limit(content, 2000, "Content")
    embeds = build_embeds(payload.get("embeds"))
    view = build_view(payload.get("buttons"))
    mentions = build_allowed_mentions(guild, payload.get("mention_roles"), bool(payload.get("mention_everyone", False)))
    if not content and not embeds:
        raise AnnouncementError("Write some content or add an embed.")
    return BuiltMessage(content=content, embeds=embeds, view=view, mentions=mentions)


# ---------- channel / permission checks ----------


def resolve_channel(guild: discord.Guild, channel_id: Any) -> Any:
    try:
        channel = guild.get_channel(int(channel_id))
    except (TypeError, ValueError):
        raise AnnouncementError("Pick a channel.")
    if channel is None or str(channel.type) not in {"text", "news"}:
        raise AnnouncementError("Pick a text or announcement channel in this server.")
    return channel


def check_bot_can(guild: discord.Guild, channel: Any, *, embed: bool, edit: bool) -> None:
    perms = channel.permissions_for(guild.me)
    needed = [("View Channel", perms.view_channel)]
    needed.append(("Read Message History", perms.read_message_history) if edit else ("Send Messages", perms.send_messages))
    if embed:
        needed.append(("Embed Links", perms.embed_links))
    missing = [name for name, ok in needed if not ok]
    if missing:
        raise AnnouncementError(f"The bot is missing {', '.join(missing)} in #{channel.name}.")


# ---------- sending ----------


async def send_built_message(channel: Any, built: BuiltMessage) -> list[str]:
    """Sends content+embeds+view, splitting content across messages if a translation made it
    grow past 2000 chars. Embeds/buttons ride the first message only."""
    chunks = build_announcement_sends(built.content, []) if built.content else [(None, [])]
    message_ids = []
    for index, (chunk, _files) in enumerate(chunks):
        kwargs: dict[str, Any] = {"content": chunk, "allowed_mentions": built.mentions}
        if index == 0:
            if built.embeds:
                kwargs["embeds"] = built.embeds
            if built.view:
                kwargs["view"] = built.view
        try:
            message = await channel.send(**kwargs)
        except discord.HTTPException as error:
            raise AnnouncementError(f"Discord rejected the message: {error.text or error}")
        message_ids.append(str(message.id))
    return message_ids


def _payload_texts(payload: dict[str, Any]) -> dict[str, Any]:
    """Deep-copies the payload so translation can mutate text fields in place, leaving colors,
    URLs, ids and flags untouched."""
    return deepcopy(payload)


async def _translate_payload(cog: Any, payload: dict[str, Any], lang: str) -> dict[str, Any]:
    out = _payload_texts(payload)
    if out.get("content"):
        out["content"] = await translate_text(cog, out["content"], lang)
    for embed in out.get("embeds") or []:
        for key in ("author_name", "title", "description", "footer"):
            if embed.get(key):
                embed[key] = await translate_text(cog, embed[key], lang)
        for field in embed.get("fields") or []:
            if field.get("name"):
                field["name"] = await translate_text(cog, field["name"], lang)
            if field.get("value"):
                field["value"] = await translate_text(cog, field["value"], lang)
    for button in out.get("buttons") or []:
        if button.get("label"):
            button["label"] = await translate_text(cog, button["label"], lang)
    return out


def _swap_mentions(text: str, lang: str, cog: Any) -> str:
    return swap_role_mentions(text, lang, cog)


async def _send_one_translation(bot: Any, guild: discord.Guild, channel: Any, payload: dict[str, Any], lang: str) -> None:
    cog = bot.get_cog("AiAnnTranslation")
    if cog is None:
        raise AnnouncementError("Translation isn't available right now (AiAnnTranslation cog not loaded).")
    translated = await _translate_payload(cog, payload, lang)
    if translated.get("content"):
        translated["content"] = _swap_mentions(translated["content"], lang, cog)
    for embed in translated.get("embeds") or []:
        if embed.get("description"):
            embed["description"] = _swap_mentions(embed["description"], lang, cog)
        for field in embed.get("fields") or []:
            if field.get("value"):
                field["value"] = _swap_mentions(field["value"], lang, cog)

    built = build_message(guild, {**translated, "mention_roles": []})
    target_channel_id = getattr(bot.settings, LANGUAGE_CHANNEL_SETTINGS[lang], None)
    target = resolve_channel(guild, target_channel_id) if target_channel_id else channel
    check_bot_can(guild, target, embed=bool(built.embeds), edit=False)
    mentions = translated_role_mentions(
        [int(role_id) for role_id in payload.get("mention_roles") or []],
        lang,
        cog.settings,
        everyone=built.mentions.everyone,
    )
    await send_built_message(target, BuiltMessage(built.content, built.embeds, built.view, mentions))


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
        check_bot_can(guild, channel, embed=bool(built.embeds), edit=False)
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
