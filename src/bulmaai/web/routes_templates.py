"""Message templates (rules, support us, anything custom): edited as V2 cards, posted from the panel."""

import discord
from aiohttp import web

from bulmaai.services.cards import AnnouncementError, build_card_view, language_row
from bulmaai.services.message_templates import create_template, delete_template, get_template, list_templates, update_template
from bulmaai.services.panel_announcements import check_bot_can, resolve_channel
from bulmaai.web.core import Actor, api_error, audit, read_json, require_guild, requires


routes = web.RouteTableDef()


def _template_id(request: web.Request) -> str:
    return request.match_info["id"]


@routes.get("/api/templates")
@requires("presets.edit")
async def templates_list(request: web.Request, actor: Actor) -> web.Response:
    return web.json_response({"templates": list_templates()})


@routes.post("/api/templates")
@requires("presets.edit")
async def templates_create(request: web.Request, actor: Actor) -> web.Response:
    try:
        template = create_template(await read_json(request))
    except AnnouncementError as error:
        raise api_error(400, str(error))
    await audit(actor, "templates.create", template["id"])
    return web.json_response(template)


@routes.put("/api/templates/{id}")
@requires("presets.edit")
async def templates_update(request: web.Request, actor: Actor) -> web.Response:
    template_id = _template_id(request)
    before = get_template(template_id)
    if before is None:
        raise api_error(404, "Unknown template.")
    try:
        template = update_template(template_id, await read_json(request))
    except AnnouncementError as error:
        raise api_error(400, str(error))
    await audit(actor, "templates.update", template_id, before=before)
    return web.json_response(template)


@routes.delete("/api/templates/{id}")
@requires("presets.edit")
async def templates_delete(request: web.Request, actor: Actor) -> web.Response:
    template_id = _template_id(request)
    before = get_template(template_id)
    found, template = delete_template(template_id)
    if not found:
        raise api_error(404, "Unknown template.")
    await audit(actor, "templates.reset" if template else "templates.delete", template_id, before=before)
    return web.json_response({"template": template})


@routes.post("/api/templates/{id}/post")
@requires("presets.edit")
async def templates_post(request: web.Request, actor: Actor) -> web.Response:
    """Posts the English card, with the language buttons when the template has them."""
    template_id = _template_id(request)
    template = get_template(template_id)
    if template is None:
        raise api_error(404, "Unknown template.")
    guild = require_guild(request)
    try:
        channel = resolve_channel(guild, (await read_json(request)).get("channel_id"))
        check_bot_can(guild, channel, edit=False)
    except AnnouncementError as error:
        raise api_error(400, str(error))
    rows = [language_row(template_id)] if template["language_buttons"] else []
    try:
        message = await channel.send(
            view=build_card_view(template["languages"]["en"], extra_rows=rows),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as error:
        raise api_error(502, f"Discord refused the message: {error.text or error}")
    await audit(actor, "templates.post", template_id, channel_id=str(channel.id), message_id=str(message.id))
    return web.json_response({"jump_url": message.jump_url})
