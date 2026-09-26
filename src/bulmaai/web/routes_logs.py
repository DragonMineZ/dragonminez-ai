"""Log pages: mod cases (Audit log) and Server logs (live Discord audit log + Dyno moderation).

Bot logs and Website logs endpoints live in routes_status.py.
"""

from datetime import datetime
from typing import Any

import discord
from aiohttp import web

from bulmaai.services import mod_cases
from bulmaai.web.core import BOT, Actor, api_error, requires, user_json
from bulmaai.web.routes_moderation import _case_json, _query_int, _snowflake
from bulmaai.database.db import get_pool


routes = web.RouteTableDef()

SERVER_LOGS_DEFAULT_LIMIT = 50
SERVER_LOGS_MAX_LIMIT = 100
# Dyno bans/kicks/timeouts already show up in Discord's own audit log (Dyno as the executor), so
# skip those when pulling Dyno's mod_cases rows into the merged Server logs feed.
DYNO_ALREADY_IN_DISCORD = ("ban", "unban", "kick", "timeout", "untimeout")


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


# --- Server logs: Discord's audit log, live, merged with Dyno's mod_cases rows -----------------


def _change_value(value: Any) -> Any:
    """Stringify one audit-log diff value sensibly (roles/channels -> their name, lists -> joined)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        rendered = [_change_value(item) for item in value]
        return ", ".join(str(item) for item in rendered if item is not None) or None
    if isinstance(value, datetime):
        return value.isoformat()
    name = getattr(value, "name", None)
    if name is None:
        return str(value)
    if isinstance(value, discord.Role):
        return f"@{name}"
    if isinstance(value, discord.abc.GuildChannel):
        return f"#{name}"
    return name


def _entry_changes(entry: discord.AuditLogEntry) -> list[dict[str, Any]]:
    try:
        before, after = dict(entry.before), dict(entry.after)
    except Exception:
        return []
    keys = sorted(set(before) | set(after))
    return [{"key": key, "before": _change_value(before.get(key)), "after": _change_value(after.get(key))} for key in keys]


def _entry_target(entry: discord.AuditLogEntry) -> dict[str, Any] | None:
    try:
        target = entry.target
    except Exception:
        return None
    if target is None:
        return None
    if isinstance(target, (discord.Member, discord.User)):
        return user_json(target)
    return {"type": type(target).__name__, "id": str(getattr(target, "id", "")) or None, "name": getattr(target, "name", None)}


def _discord_entry_json(entry: discord.AuditLogEntry, dyno_user_id: int) -> dict[str, Any]:
    action = entry.action.name if isinstance(entry.action, discord.AuditLogAction) else str(entry.action)
    return {
        "id": str(entry.id),
        "created_at": entry.created_at.isoformat(),
        "action": action,
        "executor": user_json(entry.user) if entry.user else None,
        "target": _entry_target(entry),
        "reason": entry.reason,
        "changes": _entry_changes(entry),
        "source": "dyno" if entry.user and entry.user.id == dyno_user_id else "discord",
    }


def _dyno_case_json(bot: discord.Bot, guild: discord.Guild, row: Any) -> dict[str, Any]:
    moderator_id = row["moderator_id"]
    moderator = (guild.get_member(moderator_id) or bot.get_user(moderator_id)) if moderator_id else None
    target = guild.get_member(row["user_id"]) or bot.get_user(row["user_id"])
    return {
        "id": f"case-{row['id']}",
        "created_at": row["created_at"].isoformat(),
        "action": row["action"],
        "executor": user_json(moderator) if moderator else (str(moderator_id) if moderator_id else None),
        "target": user_json(target) if target else str(row["user_id"]),
        "reason": row["reason"],
        "changes": [],
        "source": "dyno",
    }


@routes.get("/api/server-logs/actions")
@requires("logs.view")
async def server_log_actions(request: web.Request, actor: Actor) -> web.Response:
    return web.json_response({"actions": sorted(a.name for a in discord.AuditLogAction)})


@routes.get("/api/server-logs")
@requires("logs.view")
async def server_logs(request: web.Request, actor: Actor) -> web.Response:
    bot = request.app[BOT]
    guild = actor.member.guild
    limit = _query_int(request, "limit", SERVER_LOGS_DEFAULT_LIMIT, SERVER_LOGS_MAX_LIMIT)

    raw_before = request.query.get("before", "").strip()
    before_dt = None
    if raw_before:
        try:
            before_dt = datetime.fromisoformat(raw_before.replace("Z", "+00:00"))
        except ValueError:
            raise api_error(400, "before must be an ISO timestamp.")

    raw_action = request.query.get("action", "").strip()
    action_enum = None
    if raw_action:
        action_enum = getattr(discord.AuditLogAction, raw_action, None)
        if not isinstance(action_enum, discord.AuditLogAction):
            raise api_error(400, "Unknown action name.")

    raw_user = request.query.get("user", "").strip()
    moderator_id: int | None = None
    if raw_user:
        if raw_user.isdigit():
            moderator_id = int(raw_user)
        else:
            member = _match_member(guild, raw_user)
            if member is None:
                return web.json_response({"entries": [], "next_cursor": None})
            moderator_id = member.id

    try:
        discord_entries = [
            entry
            async for entry in guild.audit_logs(
                limit=limit,
                before=before_dt,
                user=discord.Object(id=moderator_id) if moderator_id is not None else None,
                action=action_enum,
            )
        ]
    except discord.Forbidden:
        raise api_error(403, "The bot is missing the 'View Audit Log' permission; grant it in Discord server settings.")

    exhausted = len(discord_entries) < limit  # fewer than asked for -> Discord's own history is done
    lower_bound = None if exhausted else discord_entries[-1].created_at

    conditions = ["guild_id = $1", "source = 'dyno'"]
    args: list[Any] = [guild.id]
    args.append(list(DYNO_ALREADY_IN_DISCORD))
    conditions.append(f"action <> ALL(${len(args)}::text[])")
    if before_dt is not None:
        args.append(before_dt)
        conditions.append(f"created_at < ${len(args)}")
    if lower_bound is not None:
        args.append(lower_bound)
        conditions.append(f"created_at >= ${len(args)}")
    if raw_action:
        args.append(raw_action)
        conditions.append(f"action = ${len(args)}")
    if moderator_id is not None:
        args.append(moderator_id)
        conditions.append(f"moderator_id = ${len(args)}")
    sql = f"SELECT * FROM mod_cases WHERE {' AND '.join(conditions)} ORDER BY created_at DESC"
    if exhausted:
        sql += " LIMIT 500"  # Discord's side is done; cap the legacy Dyno-only backlog dump
    pool = await get_pool()
    dyno_rows = await pool.fetch(sql, *args)

    dyno_user_id = bot.settings.dyno_user_id
    combined = [(entry.created_at, _discord_entry_json(entry, dyno_user_id)) for entry in discord_entries]
    combined += [(row["created_at"], _dyno_case_json(bot, guild, row)) for row in dyno_rows]
    combined.sort(key=lambda pair: pair[0], reverse=True)

    return web.json_response(
        {
            "entries": [item for _, item in combined],
            "next_cursor": None if exhausted else lower_bound.isoformat(),
        }
    )
