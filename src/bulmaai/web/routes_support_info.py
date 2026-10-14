"""Support info (cogs/support_info.py): known issues and custom answers the support AI reads, edited from the panel."""

from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from bulmaai.services import support_info
from bulmaai.services.support_info import SupportInfo
from bulmaai.web.core import BOT, Actor, api_error, audit, read_json, requires


routes = web.RouteTableDef()

COG = "SupportInfoCog"
LISTS = {
    "active": ("active",),
    "suggested": ("suggested",),
    "closed": ("removed", "discarded"),
}


def _json(entry: SupportInfo) -> dict[str, Any]:
    expired = entry.expires_at is not None and entry.expires_at <= datetime.now(timezone.utc)
    return {
        "id": entry.id,
        "kind": entry.kind,
        "title": entry.title,
        "body": entry.body,
        "status": entry.status,
        "source": entry.source,
        "ticket_id": entry.ticket_id,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        "expired": expired,
        "created_by": str(entry.created_by) if entry.created_by else None,
        "updated_by": str(entry.updated_by) if entry.updated_by else None,
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }


def _info_id(request: web.Request) -> int:
    try:
        return int(request.match_info["id"])
    except ValueError:
        raise api_error(404, "Unknown entry.")


def _fields(payload: dict[str, Any]) -> tuple[str, str, str, datetime | None]:
    kind, title, body = payload.get("kind"), payload.get("title"), payload.get("body")
    if not all(isinstance(value, str) for value in (kind, title, body)):
        raise api_error(400, "Kind, title and text are required.")
    try:
        title, body = support_info.validate(kind, title, body)
    except ValueError as error:
        raise api_error(400, str(error))
    raw = payload.get("expires_at")
    if raw in (None, "") or kind != support_info.KIND_TEMPORARY:
        return kind, title, body, None
    try:
        expires_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise api_error(400, "Expiry date isn't valid.")
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise api_error(400, "Expiry date must be in the future.")
    return kind, title, body, expires_at


@routes.get("/api/support-info")
@requires("support_info.edit")
async def support_info_list(request: web.Request, actor: Actor) -> web.Response:
    group = request.query.get("status", "active")
    if group not in LISTS:
        raise api_error(400, "Unknown status.")
    entries = [entry for status in LISTS[group] for entry in await support_info.list_by_status(status)]
    entries.sort(key=lambda entry: entry.updated_at if group == "closed" else entry.created_at, reverse=True)
    return web.json_response({"entries": [_json(entry) for entry in entries]})


@routes.post("/api/support-info")
@requires("support_info.edit")
async def support_info_create(request: web.Request, actor: Actor) -> web.Response:
    kind, title, body, expires_at = _fields(await read_json(request))
    entry = await support_info.add_info(kind=kind, title=title, body=body, expires_at=expires_at, created_by=actor.id)
    await audit(actor, "support_info.create", str(entry.id), kind=kind, title=title)
    return web.json_response(_json(entry))


@routes.put("/api/support-info/{id}")
@requires("support_info.edit")
async def support_info_update(request: web.Request, actor: Actor) -> web.Response:
    info_id = _info_id(request)
    kind, title, body, expires_at = _fields(await read_json(request))
    before = await support_info.get_info(info_id)
    entry = await support_info.update_info(
        info_id, kind=kind, title=title, body=body, expires_at=expires_at, updated_by=actor.id
    )
    if entry is None:
        raise api_error(404 if before is None else 409, "Unknown entry." if before is None else "Already handled.")
    await audit(actor, "support_info.update", str(info_id), before={"kind": before.kind, "title": before.title, "body": before.body})
    return web.json_response(_json(entry))


@routes.post("/api/support-info/{id}/remove")
@requires("support_info.edit")
async def support_info_remove(request: web.Request, actor: Actor) -> web.Response:
    info_id = _info_id(request)
    entry = await support_info.set_status(info_id, "removed", updated_by=actor.id, only_from=("active", "suggested"))
    if entry is None:
        raise api_error(409, "Already handled.")
    await audit(actor, "support_info.remove", str(info_id), title=entry.title)
    return web.json_response(_json(entry))


async def _review(request: web.Request, actor: Actor, status: str, action: str) -> web.Response:
    info_id = _info_id(request)
    cog = request.app[BOT].get_cog(COG)
    finish = getattr(cog, "finish_suggestion", None)
    if finish is None:
        raise api_error(503, "Support info isn't loaded right now.")
    entry = await finish(info_id, status=status, by_id=actor.id)
    if entry is None:
        raise api_error(409, "Already handled.")
    await audit(actor, action, str(info_id), title=entry.title)
    return web.json_response(_json(entry))


@routes.post("/api/support-info/{id}/approve")
@requires("support_info.edit")
async def support_info_approve(request: web.Request, actor: Actor) -> web.Response:
    return await _review(request, actor, "active", "support_info.approve")


@routes.post("/api/support-info/{id}/discard")
@requires("support_info.edit")
async def support_info_discard(request: web.Request, actor: Actor) -> web.Response:
    return await _review(request, actor, "discarded", "support_info.discard")
