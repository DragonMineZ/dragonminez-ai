"""Log pages: mod cases (Audit log) and Flagged joiners. Website logs endpoints live in routes_status.py."""

from typing import Any

import discord
from aiohttp import web

from bulmaai.services import joiner_alerts, mod_cases
from bulmaai.web.core import BOT, Actor, api_error, requires, user_json
from bulmaai.web.routes_moderation import _cached_user, _case_json, _query_int, _snowflake
from bulmaai.database.db import get_pool


routes = web.RouteTableDef()
# Dyno bans/kicks/timeouts already show up in Discord's own audit log (Dyno as the executor), so
# skip those when pulling Dyno's mod_cases rows into the merged Server logs feed.


def _match_member(guild: discord.Guild, query: str) -> discord.Member | None:
    """Same matching as /api/users/search; the first hit wins when the query is ambiguous."""
    q = query.lower()
    for member in guild.members:
        names = (member.name, member.display_name, getattr(member, "global_name", None) or "")
        if q == str(member.id) or any(q in name.lower() for name in names):
            return member
    return None


@routes.get("/api/cases")
@requires("mod.cases.view")
async def list_cases(request: web.Request, actor: Actor) -> web.Response:
    bot = request.app[BOT]
    guild = actor.member.guild
    action = request.query.get("action", "").strip() or None
    source = request.query.get("source", "").strip() or None
    if (action and len(action) > 32) or (source and len(source) > 32):
        raise api_error(400, "Invalid filter.")

    def _person(param: str) -> int | None:
        raw = request.query.get(param, "").strip()
        if not raw:
            return None
        if raw.isdigit():
            return _snowflake(raw)
        member = _match_member(guild, raw)
        return member.id if member else 0  # no match -> guaranteed-empty result, not an error

    limit = _query_int(request, "limit", 50, 100)
    cases = await mod_cases.list_cases(
        guild.id,
        user_id=_person("user_id"),
        moderator_id=_person("moderator_id"),
        action=action,
        source=source,
        before_id=_query_int(request, "before_id", None, 2**63 - 1),
        limit=limit,
    )
    return web.json_response(
        {
            "cases": [_case_json(bot, guild, case) for case in cases],
            "next_before_id": cases[-1].id if len(cases) == limit else None,
        }
    )


@routes.get("/api/cases/actions")
@requires("mod.cases.view")
async def list_case_actions(request: web.Request, actor: Actor) -> web.Response:
    pool = await get_pool()
    rows = await pool.fetch("SELECT DISTINCT action FROM mod_cases WHERE guild_id = $1", actor.member.guild.id)
    return web.json_response({"actions": sorted(row["action"] for row in rows)})


# --- Flagged joiners (raid_guard's new-account / returning-offender alerts) ---------------------


def _joiner_alert_json(bot: discord.Bot, guild: discord.Guild, alert: joiner_alerts.JoinerAlert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "user_id": str(alert.user_id),
        "user": user_json(_cached_user(bot, guild, alert.user_id)),
        "reason": alert.reason,
        "action_taken": alert.action_taken,
        "outcome": alert.outcome,
        "reviewed_by": str(alert.reviewed_by) if alert.reviewed_by else None,
        "reviewer": user_json(_cached_user(bot, guild, alert.reviewed_by)),
        "reviewed_at": alert.reviewed_at.isoformat() if alert.reviewed_at else None,
        "expires_at": alert.expires_at.isoformat(),
        "created_at": alert.created_at.isoformat(),
    }


@routes.get("/api/joiner-alerts")
@requires("mod.cases.view")
async def list_joiner_alerts(request: web.Request, actor: Actor) -> web.Response:
    bot = request.app[BOT]
    guild = actor.member.guild
    limit = _query_int(request, "limit", 50, 100)
    raw_user = request.query.get("user_id", "").strip()
    user_id = None
    if raw_user:
        if raw_user.isdigit():
            user_id = _snowflake(raw_user)
        else:
            member = _match_member(guild, raw_user)
            user_id = member.id if member else 0  # no match -> guaranteed-empty result, not an error
    alerts = await joiner_alerts.list_alerts(
        guild.id, user_id=user_id, before_id=_query_int(request, "before_id", None, 2**63 - 1), limit=limit
    )
    return web.json_response(
        {
            "alerts": [_joiner_alert_json(bot, guild, alert) for alert in alerts],
            "next_before_id": alerts[-1].id if len(alerts) == limit else None,
        }
    )


# --- Server logs: Discord's audit log, live, merged with Dyno's mod_cases rows -----------------
