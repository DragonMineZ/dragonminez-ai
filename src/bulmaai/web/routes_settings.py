from collections.abc import Sequence
from typing import Any, get_args, get_origin

from aiohttp import web

from bulmaai.config import (
    Settings,
    _unwrap_optional,
    get_editable_setting_names,
    load_settings,
    load_settings_overrides,
    reset_setting_override,
    set_setting_override,
)
from bulmaai.web.core import BOT, Actor, api_error, audit, read_json, requires


routes = web.RouteTableDef()


def _kind(name: str) -> tuple[str, bool]:
    target, optional = _unwrap_optional(Settings.__annotations__[name])
    if get_origin(target) in {list, tuple, Sequence} or target is Sequence:
        item = (get_args(target) or (str,))[0]
        return ("int_list" if item is int else "str_list"), optional
    return {bool: "bool", int: "int", float: "float"}.get(target, "str"), optional


def _hint(name: str) -> str | None:
    for suffix, hint in (("channel_id", "channel"), ("role_id", "role"), ("user_id", "user")):
        if name.endswith(suffix) or name.endswith(suffix + "s"):
            return hint
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [str(item) if isinstance(item, int) and not isinstance(item, bool) else item for item in value]
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53:
        return str(value)  # snowflakes lose precision as JS numbers
    return value


OWNER_ONLY_SETTINGS = {"initial_extensions", "log_level", "discord_staff_role_ids", "dev_guild_id"}


def _owner_only(name: str) -> bool:
    """Panel access and infrastructure (database, loaded code, paths, URLs, logging, staff roles)."""
    return (
        name.startswith(("panel_", "PG", "release_webhook_", "discord_log_"))
        or name.lower().endswith(("_path", "_url"))
        or name in OWNER_ONLY_SETTINGS
    )


def _check_editable(actor: Actor, name: str) -> None:
    if name not in get_editable_setting_names():
        raise api_error(404, "Unknown setting.")
    if _owner_only(name) and not actor.can("settings.edit_panel"):
        raise api_error(403, "Only the owner can change panel access and infrastructure settings.")


@routes.get("/api/settings")
@requires("settings.view")
async def list_settings(request: web.Request, actor: Actor) -> web.Response:
    current = request.app[BOT].settings
    defaults = load_settings(include_overrides=False)
    overrides = load_settings_overrides()
    items = []
    for name in get_editable_setting_names():
        kind, optional = _kind(name)
        items.append(
            {
                "name": name,
                "kind": kind,
                "optional": optional,
                "hint": _hint(name),
                "value": _jsonable(getattr(current, name)),
                "default": _jsonable(getattr(defaults, name)),
                "overridden": name in overrides,
                "owner_only": _owner_only(name),
            }
        )
    return web.json_response({"settings": items})


@routes.put("/api/settings/{name}")
@requires("settings.edit")
async def set_setting(request: web.Request, actor: Actor) -> web.Response:
    name = request.match_info["name"]
    _check_editable(actor, name)
    raw = (await read_json(request)).get("value")
    if not isinstance(raw, str):
        raise api_error(400, "value must be a string (lists: comma-separated).")
    bot = request.app[BOT]
    before = getattr(bot.settings, name)
    try:
        saved = set_setting_override(name, raw)
    except ValueError as error:
        raise api_error(400, f"Invalid value: {error}")
    bot.reload_settings()
    await audit(actor, "settings.set", name, before=before, after=saved)
    return web.json_response({"ok": True, "value": _jsonable(getattr(bot.settings, name))})


@routes.delete("/api/settings/{name}")
@requires("settings.edit")
async def reset_setting(request: web.Request, actor: Actor) -> web.Response:
    name = request.match_info["name"]
    _check_editable(actor, name)
    bot = request.app[BOT]
    before = getattr(bot.settings, name)
    reset_setting_override(name)
    bot.reload_settings()
    await audit(actor, "settings.reset", name, before=before, after=getattr(bot.settings, name))
    return web.json_response({"ok": True, "value": _jsonable(getattr(bot.settings, name))})
