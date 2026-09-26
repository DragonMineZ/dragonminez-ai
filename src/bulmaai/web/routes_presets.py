"""Message presets (rules / support-us embeds) and announcements sent as the bot."""

import re
from typing import Any
from urllib.parse import urlparse

import discord
from aiohttp import web

from bulmaai.services.message_presets import DEFAULT_MESSAGE_PRESETS, load_message_presets, replace_preset, reset_preset
from bulmaai.ui.rules_views import build_rules_embeds
from bulmaai.ui.support_views import build_support_embeds
from bulmaai.web.core import BOT, Actor, api_error, audit, read_json, require_guild, requires


routes = web.RouteTableDef()

# The bot renders presets through these builders; validating a draft by rendering it with them
# guarantees the panel only saves what /rules setup and /supportus setup can actually post.
BUILDERS = {"rules": build_rules_embeds, "support": build_support_embeds}
SUPPORT_KEYS = tuple(DEFAULT_MESSAGE_PRESETS["support"]["en"])
SUPPORT_BUTTON_KEYS = ("patreon_label", "github_label")
EMBED_KEYS = ("title", "description", "color", "url", "image_url", "thumbnail_url", "footer")
EDITABLE_EMBED_PARTS = {"type", "title", "description", "color", "url", "image", "thumbnail", "footer", "fields"}
MESSAGE_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})")


# ---------- shared validation ----------


def _limit(value: str | None, maximum: int, label: str) -> None:
    if value and len(value) > maximum:
        raise api_error(400, f"{label} is {len(value)} characters; Discord allows {maximum}.")


def _text(value: Any, label: str, *, optional: bool = False, strip: bool = True) -> str | None:
    if value is None or value == "":
        if optional:
            return None
        raise api_error(400, f"{label} can't be empty.")
    if not isinstance(value, str):
        raise api_error(400, f"{label} must be text.")
    if not value.strip():
        if optional:
            return None
        raise api_error(400, f"{label} can't be empty.")
    return value.strip() if strip else value


def _check_embeds(embeds: list[discord.Embed]) -> None:
    if len(embeds) > 10:
        raise api_error(400, f"That renders {len(embeds)} embeds; Discord allows 10 per message.")
    for number, embed in enumerate(embeds, 1):
        where = f"Embed {number}" if len(embeds) > 1 else "Embed"
        raw = embed.to_dict()
        _limit(raw.get("title"), 256, f"{where} title")
        _limit(raw.get("description"), 4096, f"{where} description")
        _limit(raw.get("footer", {}).get("text"), 2048, f"{where} footer")
        fields = raw.get("fields", [])
        if len(fields) > 25:
            raise api_error(400, f"{where} has {len(fields)} fields; Discord allows 25.")
        for field in fields:
            _limit(field["name"], 256, f"{where} field name “{field['name'][:40]}”")
            _limit(field["value"], 1024, f"{where} field “{field['name'][:40]}”")
    total = sum(len(embed) for embed in embeds)
    if total > 6000:
        raise api_error(400, f"Embeds total {total} characters; Discord allows 6000 per message.")


# ---------- presets ----------


def _clean_rules(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"title", "sections"}:
        raise api_error(400, "A rules preset needs exactly: title, sections.")
    sections = data["sections"]
    if not isinstance(sections, list) or not sections:
        raise api_error(400, "A rules preset needs at least one section.")
    cleaned = []
    for number, section in enumerate(sections, 1):
        if not isinstance(section, dict) or not set(section) <= {"title", "content"}:
            raise api_error(400, f"Section {number} must only have a title and content.")
        cleaned.append(
            {
                "title": _text(section.get("title"), f"Section {number} title", optional=True),
                "content": _text(section.get("content"), f"Section {number} content"),
            }
        )
    return {"title": _text(data["title"], "Title"), "sections": cleaned}


def _clean_support(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != set(SUPPORT_KEYS):
        raise api_error(400, f"A support preset needs exactly these fields: {', '.join(SUPPORT_KEYS)}.")
    cleaned = {key: _text(data[key], key) for key in SUPPORT_KEYS}
    for key in SUPPORT_BUTTON_KEYS:
        _limit(cleaned[key], 80, f"Button label {key}")
    return cleaned


def _validated(kind: str, language: str, data: Any) -> dict[str, Any]:
    cleaned = _clean_rules(data) if kind == "rules" else _clean_support(data)
    _check_embeds(BUILDERS[kind](language, cleaned))
    return cleaned


def _preset_ref(request: web.Request) -> tuple[str, str]:
    kind, language = request.match_info["kind"], request.match_info["language"]
    # Only languages the bot has buttons for; anything else would never be shown.
    if language not in DEFAULT_MESSAGE_PRESETS.get(kind, {}) or kind not in BUILDERS:
        raise api_error(404, "Unknown preset.")
    return kind, language


def _preset_json(kind: str, language: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "language": language,
        "data": data,
        "customized": data != DEFAULT_MESSAGE_PRESETS[kind][language],
        "embeds": [embed.to_dict() for embed in BUILDERS[kind](language, data)],
        "buttons": [data[key] for key in SUPPORT_BUTTON_KEYS] if kind == "support" else [],
    }


def _all_presets() -> list[dict[str, Any]]:
    presets = load_message_presets()
    return [
        _preset_json(kind, language, presets[kind][language])
        for kind in BUILDERS
        for language in DEFAULT_MESSAGE_PRESETS[kind]
    ]


@routes.get("/api/presets")
@requires("presets.edit")
async def list_presets(request: web.Request, actor: Actor) -> web.Response:
    return web.json_response({"presets": _all_presets()})


@routes.post("/api/presets/{kind}/{language}/preview")
@requires("presets.edit")
async def preview_preset(request: web.Request, actor: Actor) -> web.Response:
    kind, language = _preset_ref(request)
    data = _validated(kind, language, (await read_json(request)).get("data"))
    return web.json_response(_preset_json(kind, language, data))


@routes.put("/api/presets/{kind}/{language}")
@requires("presets.edit")
async def save_preset(request: web.Request, actor: Actor) -> web.Response:
    kind, language = _preset_ref(request)
    data = _validated(kind, language, (await read_json(request)).get("data"))
    before = load_message_presets()[kind][language]
    saved = replace_preset(kind, language, data)
    await audit(actor, "presets.update", f"{kind}/{language}", before=before)
    return web.json_response(_preset_json(kind, language, saved))


@routes.delete("/api/presets/{kind}/{language}")
@requires("presets.edit")
async def reset_preset_route(request: web.Request, actor: Actor) -> web.Response:
    kind, language = _preset_ref(request)
    before = load_message_presets()[kind][language]
    saved = reset_preset(kind, language)
    await audit(actor, "presets.reset", f"{kind}/{language}", before=before)
    return web.json_response(_preset_json(kind, language, saved))


# ---------- announcements ----------


def _url(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(value) > 2048:
        raise api_error(400, f"{label} must be an http(s) link.")
    return value


def _build_message(payload: dict[str, Any]) -> tuple[str | None, discord.Embed | None]:
    content = _text(payload.get("content"), "Content", optional=True, strip=False)
    _limit(content, 2000, "Content")
    raw = payload.get("embed") or {}
    if not isinstance(raw, dict) or set(raw) - set(EMBED_KEYS):
        raise api_error(400, f"embed may only contain: {', '.join(EMBED_KEYS)}.")
    values = {
        key: _text(raw.get(key), f"Embed {key}", optional=True, strip=key != "description") for key in EMBED_KEYS
    }
    if not content and not (values["title"] or values["description"]):
        raise api_error(400, "Write some content or give the embed a title or description.")
    if not any(values.values()):
        return content, None

    color = None
    if values["color"]:
        match = HEX_COLOR.fullmatch(values["color"])
        if not match:
            raise api_error(400, "Embed color must be a hex code like #F39C12.")
        color = int(match[1], 16)
    embed = discord.Embed(
        title=values["title"],
        description=values["description"],
        url=_url(values["url"], "Embed URL"),
        color=color,
    )
    if values["image_url"]:
        embed.set_image(url=_url(values["image_url"], "Image URL"))
    if values["thumbnail_url"]:
        embed.set_thumbnail(url=_url(values["thumbnail_url"], "Thumbnail URL"))
    if values["footer"]:
        embed.set_footer(text=values["footer"])
    _check_embeds([embed])
    return content, embed


def _mentions(payload: dict[str, Any]) -> tuple[discord.AllowedMentions, bool]:
    allow = payload.get("allow_pings", False)
    if not isinstance(allow, bool):
        raise api_error(400, "allow_pings must be true or false.")
    return (discord.AllowedMentions.all() if allow else discord.AllowedMentions.none()), allow


def _channel(guild: discord.Guild, channel_id: Any) -> Any:
    try:
        channel = guild.get_channel(int(channel_id))
    except (TypeError, ValueError):
        raise api_error(400, "Pick a channel.")
    if channel is None or str(channel.type) not in {"text", "news"}:
        raise api_error(400, "Pick a text or announcement channel in this server.")
    return channel


def _check_bot_can(guild: discord.Guild, channel: Any, *, embed: bool, edit: bool) -> None:
    perms = channel.permissions_for(guild.me)
    needed = [("View Channel", perms.view_channel)]
    needed.append(("Read Message History", perms.read_message_history) if edit else ("Send Messages", perms.send_messages))
    if embed:
        needed.append(("Embed Links", perms.embed_links))
    missing = [name for name, ok in needed if not ok]
    if missing:
        raise api_error(403, f"The bot is missing {', '.join(missing)} in #{channel.name}.")


def _embed_form(embed: discord.Embed) -> dict[str, str]:
    raw = embed.to_dict()
    return {
        "title": raw.get("title", ""),
        "description": raw.get("description", ""),
        "color": f"#{raw['color']:06X}" if raw.get("color") is not None else "",
        "url": raw.get("url", ""),
        "image_url": raw.get("image", {}).get("url", ""),
        "thumbnail_url": raw.get("thumbnail", {}).get("url", ""),
        "footer": raw.get("footer", {}).get("text", ""),
    }


async def _own_message(request: web.Request, channel: Any, message_id: int) -> tuple[discord.Message, dict | None]:
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        raise api_error(404, "No message with that ID in that channel.")
    except discord.HTTPException as error:
        raise api_error(502, f"Discord refused to fetch the message: {error.text or error}")
    if message.author.id != request.app[BOT].user.id:
        raise api_error(403, "Only messages the bot sent can be edited.")
    rich = [embed for embed in message.embeds if embed.type == "rich"]
    # Anything the form can't show would be silently dropped by the PATCH, so refuse up front.
    if len(rich) > 1 or any(embed.fields or set(embed.to_dict()) - EDITABLE_EMBED_PARTS for embed in rich):
        raise api_error(409, "This message has several embeds or embed fields the editor can't keep; edit it where it was posted from.")
    return message, (_embed_form(rich[0]) if rich else None)


def _message_id(value: Any) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise api_error(400, "Paste a message link or ID.")
    return int(value)


@routes.get("/api/announce/presets")
@requires("announce.send")
async def announce_presets(request: web.Request, actor: Actor) -> web.Response:
    return web.json_response({"presets": _all_presets()})


@routes.post("/api/announce")
@requires("announce.send")
async def send_announcement(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    payload = await read_json(request)
    channel = _channel(guild, payload.get("channel_id"))
    content, embed = _build_message(payload)
    mentions, pings = _mentions(payload)
    _check_bot_can(guild, channel, embed=embed is not None, edit=False)
    try:
        message = await channel.send(content=content, embed=embed, allowed_mentions=mentions)
    except discord.HTTPException as error:
        raise api_error(502, f"Discord rejected the message: {error.text or error}")
    await audit(actor, "announce.send", str(channel.id), message_id=str(message.id), pings=pings)
    return web.json_response({"channel_id": str(channel.id), "message_id": str(message.id), "jump_url": message.jump_url})


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
    channel = _channel(guild, channel_id)
    _check_bot_can(guild, channel, embed=False, edit=True)
    message, embed = await _own_message(request, channel, _message_id(ref))
    return web.json_response(
        {
            "channel_id": str(channel.id),
            "message_id": str(message.id),
            "jump_url": message.jump_url,
            "content": message.content or "",
            "embed": embed,
        }
    )


@routes.patch("/api/announce/{channel_id}/{message_id}")
@requires("announce.send")
async def edit_announcement(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    channel = _channel(guild, request.match_info["channel_id"])
    message_id = _message_id(request.match_info["message_id"])
    payload = await read_json(request)
    content, embed = _build_message(payload)
    mentions, pings = _mentions(payload)
    _check_bot_can(guild, channel, embed=embed is not None, edit=True)
    message, _ = await _own_message(request, channel, message_id)
    try:
        await message.edit(content=content, embeds=[embed] if embed else [], allowed_mentions=mentions)
    except discord.HTTPException as error:
        raise api_error(502, f"Discord rejected the edit: {error.text or error}")
    await audit(actor, "announce.edit", str(channel.id), message_id=str(message.id), pings=pings)
    return web.json_response({"channel_id": str(channel.id), "message_id": str(message.id), "jump_url": message.jump_url})
