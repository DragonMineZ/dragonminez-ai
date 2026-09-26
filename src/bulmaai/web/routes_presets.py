"""Message presets: the rules and support-us embeds posted by /rules setup and /supportus setup."""

from typing import Any

import discord
from aiohttp import web

from bulmaai.services.message_presets import DEFAULT_MESSAGE_PRESETS, load_message_presets, replace_preset, reset_preset
from bulmaai.ui.rules_views import build_rules_embeds
from bulmaai.ui.support_views import build_support_embeds
from bulmaai.web.core import Actor, api_error, audit, read_json, requires


routes = web.RouteTableDef()

# The bot renders presets through these builders; validating a draft by rendering it with them
# guarantees the panel only saves what /rules setup and /supportus setup can actually post.
BUILDERS = {"rules": build_rules_embeds, "support": build_support_embeds}
SUPPORT_KEYS = tuple(DEFAULT_MESSAGE_PRESETS["support"]["en"])
SUPPORT_BUTTON_KEYS = ("patreon_label", "github_label")


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


def all_presets() -> list[dict[str, Any]]:
    """All presets rendered to JSON; also used by the Announce page's "fill from preset" picker."""
    presets = load_message_presets()
    return [
        _preset_json(kind, language, presets[kind][language])
        for kind in BUILDERS
        for language in DEFAULT_MESSAGE_PRESETS[kind]
    ]


@routes.get("/api/presets")
@requires("presets.edit")
async def list_presets(request: web.Request, actor: Actor) -> web.Response:
    return web.json_response({"presets": all_presets()})


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
