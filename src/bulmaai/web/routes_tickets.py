"""Open AI ticket channels (AI on/off) and closed-ticket transcripts."""

from typing import Any

import discord
from aiohttp import web

from bulmaai.database.db import get_pool
from bulmaai.services.ticket_ai_state import get_ai_disabled_ticket_channels, set_ticket_ai_disabled
from bulmaai.utils.permissions import is_staff
from bulmaai.web.core import BOT, Actor, api_error, audit, read_json, require_guild, requires, user_json


routes = web.RouteTableDef()

PAGE_SIZE = 25
TICKETS_COG = "AITicketsCog"
_SUMMARY_COLUMNS = (
    "id, channel_id, channel_name, requester_id, closed_by_id, resolved, ai_confidence, "
    "message_count, title, tags, knowledge_worthy, closed_at"
)


def _page(request: web.Request) -> int:
    try:
        page = int(request.query.get("page", "1"))
    except ValueError:
        raise api_error(400, "page must be an integer.")
    if page < 1:
        raise api_error(400, "page must be at least 1.")
    return page


def _ticket_channels(guild: discord.Guild, settings: Any) -> list[Any]:
    """Same rule as the cog's _is_ticket_channel: text channels under ai_ticket_category_id."""
    category_id = settings.ai_ticket_category_id
    category = guild.get_channel(category_id) if category_id else None
    return list(getattr(category, "text_channels", []) or [])


def _requester(channel: Any, settings: Any) -> discord.Member | None:
    # Ticket Tool gives the ticket owner a member overwrite; staff/bots get theirs via roles or are skipped.
    for target in getattr(channel, "overwrites", {}) or {}:
        if isinstance(target, discord.Member) and not target.bot and not is_staff(target, settings=settings):
            return target
    return None


def _person(guild: discord.Guild, user_id: int | None) -> dict[str, Any] | str | None:
    if user_id is None:
        return None
    member = guild.get_member(user_id)
    return user_json(member) if member else str(user_id)


@routes.get("/api/tickets")
@requires("tickets.view")
async def list_tickets(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    bot = request.app[BOT]
    settings = bot.settings
    cog = bot.get_cog(TICKETS_COG)
    disabled = set() if cog else await get_ai_disabled_ticket_channels()
    tickets = []
    for channel in sorted(_ticket_channels(guild, settings), key=lambda c: c.created_at, reverse=True):
        tickets.append(
            {
                "id": str(channel.id),
                "name": channel.name,
                "created_at": channel.created_at.isoformat(),
                "requester": user_json(_requester(channel, settings)),
                "ai_enabled": cog.is_ticket_ai_enabled(channel.id) if cog else channel.id not in disabled,
                "url": f"https://discord.com/channels/{guild.id}/{channel.id}",
            }
        )
    return web.json_response(
        {
            "tickets": tickets,
            "category_configured": settings.ai_ticket_category_id is not None,
            "cog_loaded": cog is not None,
        }
    )


@routes.post("/api/tickets/{channel_id}/ai")
@requires("tickets.manage")
async def toggle_ticket_ai(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    bot = request.app[BOT]
    enabled = (await read_json(request)).get("enabled")
    if not isinstance(enabled, bool):
        raise api_error(400, "enabled must be true or false.")
    channel = next(
        (c for c in _ticket_channels(guild, bot.settings) if str(c.id) == request.match_info["channel_id"]),
        None,
    )
    if channel is None:
        raise api_error(404, "That channel isn't an open AI ticket.")

    cog = bot.get_cog(TICKETS_COG)
    if cog is not None:
        await cog.set_ticket_ai_enabled(channel.id, enabled)
    else:
        # Cog not loaded: persist only; it picks this up from the DB when it next loads.
        await set_ticket_ai_disabled(channel.id, not enabled)
    await audit(actor, "tickets.ai_toggle", str(channel.id), channel=channel.name, enabled=enabled)
    return web.json_response({"ok": True, "ai_enabled": enabled})


@routes.get("/api/transcripts")
@requires("tickets.view")
async def list_transcripts(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    page = _page(request)
    q = request.query.get("q", "").strip()[:200]

    args: list[Any] = []
    where = ""
    if q.isdigit() and int(q) < 2**63:
        args.append(int(q))
        where = "WHERE requester_id = $1 OR channel_id = $1 OR id = $1"
    elif q:
        args.append(q)
        where = (
            "WHERE title ILIKE '%' || $1 || '%' OR problem ILIKE '%' || $1 || '%' "
            "OR channel_name ILIKE '%' || $1 || '%' OR array_to_string(tags, ' ') ILIKE '%' || $1 || '%'"
        )
    args.extend([PAGE_SIZE + 1, (page - 1) * PAGE_SIZE])
    pool = await get_pool()
    rows = await pool.fetch(
        f"SELECT {_SUMMARY_COLUMNS} FROM ticket_transcripts {where} "
        f"ORDER BY closed_at DESC, id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return web.json_response(
        {
            "transcripts": [_summary_json(guild, row) for row in rows[:PAGE_SIZE]],
            "page": page,
            "has_more": len(rows) > PAGE_SIZE,
        }
    )


@routes.get("/api/transcripts/{id}")
@requires("tickets.view")
async def get_transcript(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    try:
        transcript_id = int(request.match_info["id"])
    except ValueError:
        raise api_error(400, "Transcript id must be an integer.")
    pool = await get_pool()
    row = await pool.fetchrow(
        f"SELECT {_SUMMARY_COLUMNS}, guild_id, problem, resolution, transcript, openai_file_id "
        "FROM ticket_transcripts WHERE id = $1",
        transcript_id,
    )
    if row is None:
        raise api_error(404, "Transcript not found.")
    data = _summary_json(guild, row)
    data.update(
        problem=row["problem"],
        resolution=row["resolution"],
        transcript=row["transcript"],
        openai_file_id=row["openai_file_id"],
        guild_id=str(row["guild_id"]) if row["guild_id"] else None,
    )
    return web.json_response(data)


def _summary_json(guild: discord.Guild, row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "channel_id": str(row["channel_id"]),
        "channel_name": row["channel_name"],
        "requester_id": str(row["requester_id"]) if row["requester_id"] else None,
        "requester": _person(guild, row["requester_id"]),
        "closed_by": _person(guild, row["closed_by_id"]),
        "resolved": row["resolved"],
        "ai_confidence": row["ai_confidence"],
        "message_count": row["message_count"],
        "title": row["title"],
        "tags": list(row["tags"] or []),
        "knowledge_worthy": row["knowledge_worthy"],
        "closed_at": row["closed_at"].isoformat(),
    }
