"""Overview/status, live logs, the panel audit log and the staff roster."""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from aiohttp import web

from bulmaai.database.db import get_pool
from bulmaai.logging_setup import LOG_BUFFER
from bulmaai.services import ai_budget
from bulmaai.web.core import (
    BOT,
    PERMISSIONS,
    Actor,
    Tier,
    api_error,
    audit,
    panel_guild,
    require_guild,
    requires,
    tier_for,
    user_json,
)


log = logging.getLogger(__name__)

routes = web.RouteTableDef()

STARTED_AT = datetime.now(timezone.utc)
PANEL_EXTENSION = "bulmaai.cogs.admin_panel"
DB_TIMEOUT_SECONDS = 3
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
AUDIT_PAGE_MAX = 200


def _int_param(request: web.Request, name: str, default: int | None = None, *, minimum: int = 0) -> int | None:
    raw = request.query.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise api_error(400, f"{name} must be an integer.")
    if value < minimum:
        raise api_error(400, f"{name} must be at least {minimum}.")
    return value


async def _db_health() -> dict[str, Any]:
    async def ping() -> None:
        pool = await get_pool()
        await pool.fetchval("SELECT 1")

    try:
        await asyncio.wait_for(ping(), timeout=DB_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"No answer within {DB_TIMEOUT_SECONDS}s."}
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": True, "error": None}


def _ai_budget(settings: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    resets_at = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    pools = [
        {
            "pool": pool,
            "used": ai_budget.used(pool),
            "limit": getattr(settings, f"openai_daily_{pool}_token_limit", None),
        }
        for pool in (ai_budget.SMALL, ai_budget.BIG, ai_budget.BILLED)
    ]
    return {"pools": pools, "paused": ai_budget.is_paused(settings), "resets_at": resets_at.isoformat()}


@routes.get("/api/status")
@requires("status.view")
async def status(request: web.Request, actor: Actor) -> web.Response:
    bot = request.app[BOT]
    guild = panel_guild(bot)
    latency = getattr(bot, "latency", None)
    latency_ms = round(latency * 1000) if isinstance(latency, float) and math.isfinite(latency) else None
    now = datetime.now(timezone.utc)
    return web.json_response(
        {
            "bot": user_json(getattr(bot, "user", None)),
            "latency_ms": latency_ms,
            "started_at": STARTED_AT.isoformat(),
            "uptime_seconds": int((now - STARTED_AT).total_seconds()),
            "guild": {"id": str(guild.id), "name": guild.name, "member_count": guild.member_count} if guild else None,
            "extensions": sorted(bot.extensions),
            "panel_extension": PANEL_EXTENSION,
            "db": await _db_health(),
            "ai_budget": _ai_budget(bot.settings),
        }
    )


@routes.post("/api/status/extensions/{name}/reload")
@requires("bot.reload")
async def reload_extension(request: web.Request, actor: Actor) -> web.Response:
    bot = request.app[BOT]
    name = request.match_info["name"]
    if name not in bot.extensions:
        raise api_error(404, "That extension isn't loaded.")
    if name == PANEL_EXTENSION:
        raise api_error(400, "The panel can't reload its own extension; restart the bot instead.")
    try:
        bot.reload_extension(name)
    except discord.ExtensionError as error:
        log.exception("Panel reload of %s failed", name)
        raise api_error(502, f"Reload failed, previous version kept: {error}")
    await audit(actor, "bot.reload", name)
    return web.json_response({"ok": True, "extensions": sorted(bot.extensions)})


@routes.get("/api/logs")
@requires("logs.view")
async def logs(request: web.Request, actor: Actor) -> web.Response:
    after = _int_param(request, "after", 0)
    level = request.query.get("level", "DEBUG").strip().upper() or "DEBUG"
    if level not in LOG_LEVELS:
        raise api_error(400, f"level must be one of {', '.join(LOG_LEVELS)}.")
    records, last_id = LOG_BUFFER.since(after, logging.getLevelName(level))
    return web.json_response({"records": records, "last_id": last_id})


@routes.get("/api/audit")
@requires("audit.view")
async def audit_log(request: web.Request, actor: Actor) -> web.Response:
    limit = min(_int_param(request, "limit", 50, minimum=1), AUDIT_PAGE_MAX)
    before_id = _int_param(request, "before_id", minimum=1)
    actor_id = _int_param(request, "actor_id", minimum=1)
    action = request.query.get("action", "").strip()

    conditions: list[str] = []
    args: list[Any] = []
    if before_id is not None:
        args.append(before_id)
        conditions.append(f"id < ${len(args)}")
    if actor_id is not None:
        args.append(actor_id)
        conditions.append(f"actor_id = ${len(args)}")
    if action:
        args.append(action)  # prefix match, so "mod." finds every moderation action
        conditions.append(f"left(action, length(${len(args)}::text)) = ${len(args)}::text")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    args.append(limit + 1)
    pool = await get_pool()
    rows = await pool.fetch(
        f"SELECT id, actor_id, action, target, details, created_at FROM panel_audit_log {where} "
        f"ORDER BY id DESC LIMIT ${len(args)}",
        *args,
    )

    guild = panel_guild(request.app[BOT])
    entries = []
    for row in rows[:limit]:
        member = guild.get_member(row["actor_id"]) if guild else None
        details = row["details"]
        entries.append(
            {
                "id": row["id"],
                "actor_id": str(row["actor_id"]),
                "actor": user_json(member) if member else str(row["actor_id"]),
                "action": row["action"],
                "target": row["target"],
                "details": json.loads(details) if isinstance(details, str) else details,
                "created_at": row["created_at"].isoformat(),
            }
        )
    return web.json_response({"entries": entries, "has_more": len(rows) > limit})


@routes.get("/api/staff")
@requires("audit.view")
async def staff(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    settings = request.app[BOT].settings
    members = []
    for member in guild.members:
        if member.bot:
            continue
        tier = tier_for(member, settings)
        if tier > Tier.NONE:
            members.append({"user": user_json(member), "tier": tier.name.lower(), "tier_level": int(tier)})
    members.sort(key=lambda m: (-m["tier_level"], m["user"]["display_name"].lower()))
    return web.json_response(
        {
            "members": members,
            "tiers": [tier.name.lower() for tier in Tier if tier > Tier.NONE],
            "permissions": [
                {"name": name, "tier": tier.name.lower(), "tier_level": int(tier)} for name, tier in PERMISSIONS.items()
            ],
            "role_ids": {
                "admin": [str(r) for r in settings.panel_admin_role_ids],
                "moderator": [str(r) for r in settings.panel_moderator_role_ids],
                "helper": [str(r) for r in settings.panel_helper_role_ids],
            },
        }
    )
