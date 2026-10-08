"""Announce composer: Components V2 card sends/edits, scheduled sends, drafts and history.

Endpoints under /api/announce*. Message building/validation and the scheduled-send claim live
in services/panel_announcements.py, shared with the scheduled_announcements cog.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

import discord
from aiohttp import web

from bulmaai.database.db import get_pool
from bulmaai.services.cards import (
    build_card_view,
    card_from_message,
    count_chars,
    count_components,
    language_row,
    language_template_id,
    normalize_card,
)
from bulmaai.services.message_templates import list_templates
from bulmaai.services.panel_announcements import (
    AnnouncementError,
    build_message,
    check_bot_can,
    resolve_channel,
    send_built_message,
    send_scheduled,
    send_translations,
)
from bulmaai.web.core import BOT, Actor, api_error, audit, read_json, require_guild, requires


log = logging.getLogger(__name__)

routes = web.RouteTableDef()

MESSAGE_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
MESSAGE_KEYS = ("card", "mention_roles", "mention_everyone", "translate")
HISTORY_LIMIT = 50


def _guard(fn: Callable, *args: Any, status: int = 400, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except AnnouncementError as error:
        raise api_error(status, str(error))


def _extract_message(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in MESSAGE_KEYS}


def _row_id(request: web.Request) -> int:
    try:
        return int(request.match_info["id"])
    except ValueError:
        raise api_error(400, "Invalid id.")


def _message_id(value: Any) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise api_error(400, "Paste a message link or ID.")
    return int(value)


def _parse_send_at(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise api_error(400, "send_at must be an ISO date string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise api_error(400, "send_at isn't a valid date/time.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise api_error(400, "Scheduled time must be in the future.")
    return parsed


def _row_json(row: Any) -> dict[str, Any]:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    message_ids = row["message_ids"]
    if isinstance(message_ids, str):
        message_ids = json.loads(message_ids) if message_ids else None
    return {
        "id": row["id"],
        "status": row["status"],
        "author_id": str(row["author_id"]),
        "channel_id": str(row["channel_id"]) if row["channel_id"] else None,
        "payload": payload,
        "send_at": row["send_at"].isoformat() if row["send_at"] else None,
        "message_ids": message_ids,
        "error": row["error"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "sent_at": row["sent_at"].isoformat() if row["sent_at"] else None,
    }


async def _own_message(request: web.Request, channel: Any, message_id: int):
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        raise api_error(404, "No message with that ID in that channel.")
    except discord.HTTPException as error:
        raise api_error(502, f"Discord refused to fetch the message: {error.text or error}")
    if message.author.id != request.app[BOT].user.id:
        raise api_error(403, "Only messages the bot sent can be edited.")
    return message


# ---------- compose / send / edit ----------


@routes.get("/api/announce/templates")
@requires("announce.send")
async def announce_templates(request: web.Request, actor: Actor) -> web.Response:
    return web.json_response({"templates": list_templates()})


@routes.post("/api/cards/preview")
@requires("announce.send")
async def preview_card(request: web.Request, actor: Actor) -> web.Response:
    card = _guard(normalize_card, (await read_json(request)).get("card"))
    return web.json_response({"card": card, "chars": count_chars(card), "components": count_components(card)})


@routes.post("/api/announce")
@requires("announce.send")
async def create_announcement(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    payload = await read_json(request)
    send_at = _parse_send_at(payload.get("send_at"))
    if not payload.get("channel_id"):
        raise api_error(400, "Pick a channel.")
    channel = _guard(resolve_channel, guild, payload.get("channel_id"))
    message = _extract_message(payload)
    built = _guard(build_message, guild, message)
    message["card"] = normalize_card(message["card"])

    pool = await get_pool()
    if send_at is not None:
        row = await pool.fetchrow(
            "INSERT INTO panel_announcements (author_id, status, channel_id, payload, send_at) "
            "VALUES ($1, 'scheduled', $2, $3::jsonb, $4) RETURNING *",
            actor.id,
            channel.id,
            json.dumps(message),
            send_at,
        )
        await audit(actor, "announce.schedule", str(channel.id), send_at=send_at.isoformat())
        return web.json_response(_row_json(row))

    _guard(check_bot_can, guild, channel, edit=False, status=403)
    row = await pool.fetchrow(
        "INSERT INTO panel_announcements (author_id, status, channel_id, payload) "
        "VALUES ($1, 'sending', $2, $3::jsonb) RETURNING id",
        actor.id,
        channel.id,
        json.dumps(message),
    )
    try:
        message_ids = await send_built_message(channel, built)
    except AnnouncementError as error:
        await pool.execute(
            "UPDATE panel_announcements SET status = 'failed', error = $2, updated_at = now() WHERE id = $1",
            row["id"],
            str(error),
        )
        raise api_error(502, str(error))

    translation_errors = await send_translations(request.app[BOT], guild, channel, message)
    if translation_errors:
        await pool.execute(
            "UPDATE panel_announcements SET status = 'sent', message_ids = $2::jsonb, sent_at = now(), "
            "updated_at = now(), payload = payload || $3::jsonb WHERE id = $1",
            row["id"],
            json.dumps({"main": message_ids}),
            json.dumps({"translation_errors": translation_errors}),
        )
    else:
        await pool.execute(
            "UPDATE panel_announcements SET status = 'sent', message_ids = $2::jsonb, sent_at = now(), "
            "updated_at = now() WHERE id = $1",
            row["id"],
            json.dumps({"main": message_ids}),
        )
    pings = bool(message.get("mention_roles")) or bool(message.get("mention_everyone"))
    await audit(actor, "announce.send", str(channel.id), message_id=message_ids[0], pings=pings)
    return web.json_response(
        {
            "id": row["id"],
            "channel_id": str(channel.id),
            "message_ids": message_ids,
            "jump_url": f"https://discord.com/channels/{guild.id}/{channel.id}/{message_ids[0]}",
            "translation_errors": translation_errors,
        }
    )


@routes.get("/api/announce/message")
@requires("announce.send")
async def load_announcement(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    ref = request.query.get("ref", "").strip()
    channel_id: Any = request.query.get("channel_id")
    link = MESSAGE_LINK.search(ref)
    if link:
        if int(link[1]) != guild.id:
            raise api_error(400, "That link points to another server.")
        channel_id, ref = link[2], link[3]
    channel = _guard(resolve_channel, guild, channel_id)
    _guard(check_bot_can, guild, channel, edit=True, status=403)
    message = await _own_message(request, channel, _message_id(ref))
    card = _guard(card_from_message, message, status=409)
    return web.json_response(
        {"channel_id": str(channel.id), "message_id": str(message.id), "jump_url": message.jump_url, "card": card}
    )


@routes.patch("/api/announce/{channel_id}/{message_id}")
@requires("announce.send")
async def edit_announcement(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    channel = _guard(resolve_channel, guild, request.match_info["channel_id"])
    message_id = _message_id(request.match_info["message_id"])
    payload = await read_json(request)
    message_obj = await _own_message(request, channel, message_id)
    _guard(check_bot_can, guild, channel, edit=True, status=403)
    template_id = language_template_id([component.to_dict() for component in message_obj.components])
    card = _guard(normalize_card, payload.get("card"), language_row=template_id is not None)
    rows = [language_row(template_id)] if template_id else []
    try:
        await message_obj.edit(view=build_card_view(card, extra_rows=rows), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as error:
        raise api_error(502, f"Discord rejected the edit: {error.text or error}")
    await audit(actor, "announce.edit", str(channel.id), message_id=str(message_obj.id))

    panel_id = payload.get("panel_id")
    if panel_id:
        try:
            pool = await get_pool()
            await pool.execute(
                "UPDATE panel_announcements SET payload = $2::jsonb, updated_at = now() WHERE id = $1",
                int(panel_id),
                json.dumps({**_extract_message(payload), "card": card}),
            )
        except Exception:
            log.exception("Failed to refresh panel_announcements history row %s after an edit", panel_id)
    return web.json_response({"channel_id": str(channel.id), "message_id": str(message_obj.id), "jump_url": message_obj.jump_url})


# ---------- scheduled ----------


@routes.get("/api/announce/scheduled")
@requires("announce.send")
async def list_scheduled(request: web.Request, actor: Actor) -> web.Response:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM panel_announcements WHERE status = 'scheduled' ORDER BY send_at ASC LIMIT 100")
    return web.json_response({"items": [_row_json(row) for row in rows]})


@routes.put("/api/announce/scheduled/{id}")
@requires("announce.send")
async def update_scheduled(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    row_id = _row_id(request)
    payload = await read_json(request)
    send_at = _parse_send_at(payload.get("send_at"))
    if send_at is None:
        raise api_error(400, "Pick a time in the future.")
    if not payload.get("channel_id"):
        raise api_error(400, "Pick a channel.")
    channel = _guard(resolve_channel, guild, payload.get("channel_id"))
    message = _extract_message(payload)
    _guard(build_message, guild, message)
    message["card"] = normalize_card(message["card"])

    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE panel_announcements SET channel_id = $2, payload = $3::jsonb, send_at = $4, updated_at = now() "
        "WHERE id = $1 AND status = 'scheduled' RETURNING *",
        row_id,
        channel.id,
        json.dumps(message),
        send_at,
    )
    if row is None:
        raise api_error(409, "Only a still-scheduled announcement can be edited.")
    await audit(actor, "announce.schedule_edit", str(row_id), send_at=send_at.isoformat())
    return web.json_response(_row_json(row))


@routes.post("/api/announce/scheduled/{id}/cancel")
@requires("announce.send")
async def cancel_scheduled(request: web.Request, actor: Actor) -> web.Response:
    row_id = _row_id(request)
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE panel_announcements SET status = 'cancelled', updated_at = now() "
        "WHERE id = $1 AND status = 'scheduled' RETURNING id",
        row_id,
    )
    if row is None:
        raise api_error(409, "Only a still-scheduled announcement can be cancelled.")
    await audit(actor, "announce.schedule_cancel", str(row_id))
    return web.json_response({"id": row_id, "status": "cancelled"})


@routes.post("/api/announce/scheduled/{id}/send-now")
@requires("announce.send")
async def send_scheduled_now(request: web.Request, actor: Actor) -> web.Response:
    row_id = _row_id(request)
    result = await send_scheduled(request.app[BOT], row_id)
    await audit(actor, "announce.send_now", str(row_id), status=result["status"])
    if result["status"] != "sent":
        raise api_error(409, result.get("error") or "Failed to send.")
    return web.json_response(result)


# ---------- history ----------


@routes.get("/api/announce/history")
@requires("announce.send")
async def list_history(request: web.Request, actor: Actor) -> web.Response:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM panel_announcements WHERE status NOT IN ('draft', 'scheduled') "
        "ORDER BY COALESCE(sent_at, updated_at) DESC LIMIT $1",
        HISTORY_LIMIT,
    )
    return web.json_response({"items": [_row_json(row) for row in rows]})


# ---------- drafts ----------


@routes.get("/api/announce/drafts")
@requires("announce.send")
async def list_drafts(request: web.Request, actor: Actor) -> web.Response:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM panel_announcements WHERE status = 'draft' ORDER BY updated_at DESC LIMIT 100")
    return web.json_response({"items": [_row_json(row) for row in rows]})


@routes.post("/api/announce/drafts")
@requires("announce.send")
async def create_draft(request: web.Request, actor: Actor) -> web.Response:
    payload = await read_json(request)
    message = _extract_message(payload)
    channel_id = payload.get("channel_id")
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO panel_announcements (author_id, status, channel_id, payload) VALUES ($1, 'draft', $2, $3::jsonb) RETURNING *",
        actor.id,
        int(channel_id) if channel_id else None,
        json.dumps(message),
    )
    await audit(actor, "announce.draft_save", str(row["id"]))
    return web.json_response(_row_json(row))


@routes.put("/api/announce/drafts/{id}")
@requires("announce.send")
async def update_draft(request: web.Request, actor: Actor) -> web.Response:
    row_id = _row_id(request)
    payload = await read_json(request)
    message = _extract_message(payload)
    channel_id = payload.get("channel_id")
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE panel_announcements SET channel_id = $2, payload = $3::jsonb, updated_at = now() "
        "WHERE id = $1 AND status = 'draft' RETURNING *",
        row_id,
        int(channel_id) if channel_id else None,
        json.dumps(message),
    )
    if row is None:
        raise api_error(404, "Draft not found.")
    await audit(actor, "announce.draft_save", str(row_id))
    return web.json_response(_row_json(row))


@routes.delete("/api/announce/drafts/{id}")
@requires("announce.send")
async def delete_draft(request: web.Request, actor: Actor) -> web.Response:
    row_id = _row_id(request)
    pool = await get_pool()
    result = await pool.execute("DELETE FROM panel_announcements WHERE id = $1 AND status = 'draft'", row_id)
    if result == "DELETE 0":
        raise api_error(404, "Draft not found.")
    await audit(actor, "announce.draft_delete", str(row_id))
    return web.json_response({"ok": True})
