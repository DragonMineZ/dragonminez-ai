"""Users page (lookup + profile), moderation actions and the mod case log."""

import asyncio
import logging
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from aiohttp import web

from bulmaai.database.db import get_pool
from bulmaai.services import automod_hits, mod_actions, mod_cases
from bulmaai.services.mod_actions import ModActionError, parse_duration_seconds
from bulmaai.services.bug_reports import list_bug_reports_by_reporter
from bulmaai.services.member_activity import get_member_activity, xp_threshold
from bulmaai.services.patreon_grants import get_patreon_link
from bulmaai.web.core import (
    BOT,
    Actor,
    api_error,
    audit,
    read_json,
    requires,
    resolve_member,
    tier_for,
    user_json,
)


log = logging.getLogger(__name__)

routes = web.RouteTableDef()

SEARCH_LIMIT = 25
MAX_REASON_LENGTH = 400  # leaves room for the " (via panel by ...)" suffix under Discord's 512 limit
MAX_TIMEOUT_MINUTES = 28 * 24 * 60
MAX_BAN_DELETE_HOURS = 168
MEMBERS_DEFAULT_LIMIT = 60
MEMBERS_MAX_LIMIT = 100
MEMBER_SORTS = ("joined_desc", "joined_asc", "name")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _snowflake(raw: str) -> int:
    if not raw.isdigit() or len(raw) > 20:
        raise api_error(400, "Invalid user id.")
    return int(raw)


def _query_int(request: web.Request, name: str, default: int | None, high: int) -> int | None:
    raw = request.query.get(name, "")
    if not raw:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= high:
        raise api_error(400, f"{name} must be a number between 1 and {high}.")
    return int(raw)


def _cached_user(bot: discord.Bot, guild: discord.Guild, user_id: int | None) -> discord.abc.User | None:
    if user_id is None:
        return None
    return guild.get_member(user_id) or bot.get_user(user_id)


def _case_json(bot: discord.Bot, guild: discord.Guild, case: mod_cases.ModCase) -> dict[str, Any]:
    return {
        "id": case.id,
        "user_id": str(case.user_id),
        "user": user_json(_cached_user(bot, guild, case.user_id)),
        "moderator_id": str(case.moderator_id) if case.moderator_id else None,
        "moderator": user_json(_cached_user(bot, guild, case.moderator_id)),
        "action": case.action,
        "reason": case.reason,
        "duration_seconds": case.duration_seconds,
        "source": case.source,
        "created_at": _iso(case.created_at),
        "active": case.active,
        "expires_at": _iso(case.expires_at),
    }


# --- Lookup -----------------------------------------------------------------------------------


@routes.get("/api/users/search")
@requires("users.view")
async def search_users(request: web.Request, actor: Actor) -> web.Response:
    query = request.query.get("q", "").strip().lower()
    if len(query) > 100:
        raise api_error(400, "Search is too long.")
    results = []
    if query:
        for member in actor.member.guild.members:
            names = (member.name, member.display_name, member.global_name or "")
            if query == str(member.id) or any(query in name.lower() for name in names):
                results.append({**user_json(member), "joined_at": _iso(member.joined_at)})
                if len(results) >= SEARCH_LIMIT:
                    break
    return web.json_response({"results": results})


def _query_offset(request: web.Request) -> int:
    raw = request.query.get("offset", "0").strip() or "0"
    if not raw.isdigit():
        raise api_error(400, "offset must be a non-negative whole number.")
    return int(raw)


def _role_id_param(request: web.Request) -> int | None:
    raw = request.query.get("role_id", "").strip()
    if not raw:
        return None
    if not raw.isdigit() or len(raw) > 20:
        raise api_error(400, "Invalid role id.")
    return int(raw)


def _top_colored_role(member: discord.Member) -> dict[str, Any] | None:
    """The highest-position role with a non-default colour, for the member grid's colour dot."""
    for role in sorted(member.roles, key=lambda r: r.position, reverse=True):
        if not role.is_default() and role.colour.value:
            return {"id": str(role.id), "name": role.name, "color": str(role.colour)}
    return None


def _member_list_json(bot: discord.Bot, member: discord.Member) -> dict[str, Any]:
    return {
        **user_json(member),
        "avatar": member.display_avatar.with_size(64).url,
        "joined_at": _iso(member.joined_at),
        "top_role": _top_colored_role(member),
        "tier": tier_for(member, bot.settings).name.lower(),
        "timed_out_until": _iso(member.communication_disabled_until) if member.timed_out else None,
    }


@routes.get("/api/members")
@requires("users.view")
async def list_members(request: web.Request, actor: Actor) -> web.Response:
    """The member grid: filter/sort the guild's cached members, paged for infinite scroll."""
    bot = request.app[BOT]
    guild = actor.member.guild
    query = request.query.get("q", "").strip().lower()
    if len(query) > 100:
        raise api_error(400, "Search is too long.")
    role_id = _role_id_param(request)
    sort = request.query.get("sort", "joined_desc")
    if sort not in MEMBER_SORTS:
        raise api_error(400, f"sort must be one of {', '.join(MEMBER_SORTS)}.")
    offset = _query_offset(request)
    limit = _query_int(request, "limit", MEMBERS_DEFAULT_LIMIT, MEMBERS_MAX_LIMIT)

    members = guild.members
    if query:
        def _matches(m: discord.Member) -> bool:
            names = (m.name, m.display_name, m.global_name or "")
            return query == str(m.id) or any(query in name.lower() for name in names)

        members = [m for m in members if _matches(m)]
    if role_id is not None:
        members = [m for m in members if any(r.id == role_id for r in m.roles)]

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    if sort == "name":
        members = sorted(members, key=lambda m: (m.display_name.lower(), m.id))
    elif sort == "joined_asc":
        members = sorted(members, key=lambda m: (m.joined_at or epoch, m.id))
    else:
        members = sorted(members, key=lambda m: (m.joined_at or epoch, m.id), reverse=True)

    total = len(members)
    page = members[offset : offset + limit]
    return web.json_response(
        {
            "members": [_member_list_json(bot, m) for m in page],
            "total": total,
            "has_more": offset + limit < total,
        }
    )


async def _fetch_user(bot: discord.Bot, user_id: int) -> discord.abc.User | None:
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException:
        raise api_error(502, "Discord didn't answer, try again.")


async def _ban_state(guild: discord.Guild, user_id: int) -> tuple[bool | None, str | None]:
    """(banned, reason); banned is None when the bot can't tell (e.g. missing Ban Members)."""
    try:
        entry = await guild.fetch_ban(discord.Object(id=user_id))
    except discord.NotFound:
        return False, None
    except discord.HTTPException:
        return None, None
    return True, entry.reason


async def _section(coro: Awaitable[Any]) -> dict[str, Any]:
    """Each profile section fails on its own so one broken query doesn't blank the page."""
    try:
        return {"data": await coro}
    except Exception:
        log.exception("User profile section failed")
        return {"error": "Couldn't load this section, check the bot logs."}


async def _activity(guild_id: int, user_id: int) -> dict[str, Any]:
    activity = await get_member_activity(guild_id, user_id)
    return {
        "xp": activity.xp,
        "level": activity.level,
        "next_level_xp": xp_threshold(activity.level + 1),
        "last_award_at": _iso(activity.last_award_at),
    }


async def _tickets(user_id: int) -> list[dict[str, Any]]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, channel_id, channel_name, title, resolved, message_count, closed_at
        FROM ticket_transcripts WHERE requester_id = $1 ORDER BY closed_at DESC LIMIT 10
        """,
        user_id,
    )
    return [
        {
            "id": row["id"],
            "channel_id": str(row["channel_id"]),
            "channel_name": row["channel_name"],
            "title": row["title"],
            "resolved": row["resolved"],
            "message_count": row["message_count"],
            "closed_at": _iso(row["closed_at"]),
        }
        for row in rows
    ]


async def _bug_reports(user_id: int) -> list[dict[str, Any]]:
    reports = await list_bug_reports_by_reporter(user_id, limit=10)
    return [
        {
            "thread_id": str(report.thread_id),
            "title": report.ai_title,
            "status": report.status,
            "repo": report.repo,
            "issue_number": report.issue_number,
            "created_at": _iso(report.created_at),
        }
        for report in reports
    ]


async def _dev_jar(user_id: int) -> dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT count(*) AS downloads, max(downloaded_at) AS last_at FROM dev_jar_user_downloads "
        "WHERE discord_user_id = $1",
        user_id,
    )
    return {"downloads": row["downloads"], "last_download_at": _iso(row["last_at"])}


async def _patreon(user_id: int) -> dict[str, Any] | None:
    link = await get_patreon_link(user_id)
    if link is None:
        return None
    return {
        "name": link.patreon_full_name,
        "status": link.patron_status,
        "entitled": link.entitlement_active,
        "tier_ids": list(link.tier_ids),
        "last_charge_date": _iso(link.last_charge_date),
    }


async def _user_cases(bot: discord.Bot, guild: discord.Guild, user_id: int) -> list[dict[str, Any]]:
    cases = await mod_cases.list_cases(guild.id, user_id=user_id, limit=25)
    return [_case_json(bot, guild, case) for case in cases]


@routes.get("/api/users/{user_id}")
@requires("users.view")
async def user_profile(request: web.Request, actor: Actor) -> web.Response:
    bot = request.app[BOT]
    guild = actor.member.guild
    user_id = _snowflake(request.match_info["user_id"])
    member = await resolve_member(guild, user_id)
    user = member or await _fetch_user(bot, user_id)
    if user is None:
        raise api_error(404, "Unknown Discord user.")
    banned, ban_reason = (False, None) if member else await _ban_state(guild, user_id)

    sections: dict[str, Awaitable[Any]] = {
        "activity": _activity(guild.id, user_id),
        "cases": _user_cases(bot, guild, user_id),
        "tickets": _tickets(user_id),
        "bug_reports": _bug_reports(user_id),
        "dev_jar": _dev_jar(user_id),
    }
    if actor.can("patreon.view"):
        sections["patreon"] = _patreon(user_id)
    results = await asyncio.gather(*(_section(coro) for coro in sections.values()))

    return web.json_response(
        {
            "user": user_json(user),
            "member": member is not None,
            "created_at": _iso(user.created_at),
            "joined_at": _iso(member.joined_at) if member else None,
            "roles": [
                {"id": str(role.id), "name": role.name, "color": str(role.colour)}
                for role in reversed(member.roles)
                if not role.is_default()
            ]
            if member
            else [],
            "timed_out_until": _iso(member.communication_disabled_until) if member and member.timed_out else None,
            "tier": tier_for(member, bot.settings).name.lower() if member else "none",
            "banned": banned,
            "ban_reason": ban_reason,
            "sections": dict(zip(sections, results)),
        }
    )


# --- Actions ----------------------------------------------------------------------------------


def _whole_number(body: dict[str, Any], name: str, default: int | None = None) -> int:
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise api_error(400, f"{name} must be a whole number.")
    return value


async def _moderate(request: web.Request, actor: Actor, action: str) -> web.Response:
    bot = request.app[BOT]
    guild = actor.member.guild
    user_id = _snowflake(request.match_info["user_id"])
    body = await read_json(request)
    reason = body.get("reason") or ""
    if not isinstance(reason, str):
        raise api_error(400, "reason must be text.")
    reason = reason.strip()
    if not reason and action != "untimeout":
        raise api_error(400, "A reason is required.")
    if len(reason) > MAX_REASON_LENGTH:
        raise api_error(400, f"Keep the reason under {MAX_REASON_LENGTH} characters.")

    details: dict[str, Any] = {"reason": reason}
    duration_seconds = None
    delete_seconds = 0
    if action == "timeout":
        minutes = _whole_number(body, "minutes")
        if minutes < 1:
            raise api_error(400, "minutes must be at least 1.")
        duration_seconds = min(minutes, MAX_TIMEOUT_MINUTES) * 60
    elif action in ("ban", "softban"):
        hours = _whole_number(body, "delete_message_hours", 0 if action == "ban" else 24)
        if not 0 <= hours <= MAX_BAN_DELETE_HOURS:
            raise api_error(400, f"delete_message_hours must be 0-{MAX_BAN_DELETE_HOURS}.")
        details["delete_message_hours"] = hours
        delete_seconds = hours * 3600
        raw_duration = body.get("duration")
        if action == "ban" and raw_duration:
            duration_seconds = parse_duration_seconds(raw_duration) if isinstance(raw_duration, str) else None
            if not duration_seconds:
                raise api_error(400, "duration must look like 12h, 7d or 2w (leave it empty for a permanent ban).")

    try:
        result = await mod_actions.perform(
            bot,
            guild,
            action=action,
            target_id=user_id,
            moderator=actor.member,
            reason=reason,
            duration_seconds=duration_seconds,
            delete_message_seconds=delete_seconds,
            source="panel",
        )
    except ModActionError as error:
        raise api_error(error.status, str(error))
    if duration_seconds:
        details["duration_seconds"] = duration_seconds
    if result.dm_sent is not None:
        details["dm_sent"] = result.dm_sent
    await audit(actor, f"mod.{action}", str(user_id), case_id=result.case_id, **details)
    return web.json_response(
        {
            "ok": True,
            "case_id": result.case_id,
            "dm_sent": result.dm_sent,
            "escalation": result.escalation.action if result.escalation else None,
        }
    )


@routes.post("/api/users/{user_id}/warn")
@requires("mod.warn")
async def warn_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "warn")


@routes.post("/api/users/{user_id}/note")
@requires("mod.warn")
async def note_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "note")


@routes.post("/api/users/{user_id}/timeout")
@requires("mod.timeout")
async def timeout_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "timeout")


@routes.post("/api/users/{user_id}/untimeout")
@requires("mod.timeout")
async def untimeout_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "untimeout")


@routes.post("/api/users/{user_id}/kick")
@requires("mod.kick")
async def kick_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "kick")


@routes.post("/api/users/{user_id}/ban")
@requires("mod.ban")
async def ban_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "ban")


@routes.post("/api/users/{user_id}/softban")
@requires("mod.ban")
async def softban_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "softban")


@routes.post("/api/users/{user_id}/unban")
@requires("mod.ban")
async def unban_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "unban")


# --- Case edits -------------------------------------------------------------------------------


def _case_id(request: web.Request) -> int:
    raw = request.match_info["case_id"]
    if not raw.isdigit() or len(raw) > 18:
        raise api_error(400, "Invalid case id.")
    return int(raw)


@routes.post("/api/cases/{case_id}/reason")
@requires("mod.cases.edit")
async def edit_case_reason(request: web.Request, actor: Actor) -> web.Response:
    case_id = _case_id(request)
    reason = (await read_json(request)).get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise api_error(400, "A reason is required.")
    reason = reason.strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise api_error(400, f"Keep the reason under {MAX_REASON_LENGTH} characters.")
    if not await mod_cases.update_reason(actor.member.guild.id, case_id, reason):
        raise api_error(404, "Unknown case.")
    await audit(actor, "mod.case_reason", str(case_id), reason=reason)
    return web.json_response({"ok": True})


@routes.delete("/api/cases/{case_id}")
@requires("mod.cases.edit")
async def remove_case(request: web.Request, actor: Actor) -> web.Response:
    """Soft-deletes a warn or note (Dyno's delwarn/delnote): it stops counting toward the warn ladder."""
    guild_id = actor.member.guild.id
    case_id = _case_id(request)
    case = await mod_cases.get_case(guild_id, case_id)
    if case is None:
        raise api_error(404, "Unknown case.")
    if case.action not in ("warn", "note"):
        raise api_error(400, "Only warnings and notes can be removed.")
    await mod_cases.deactivate_case(guild_id, case_id)
    await audit(actor, "mod.case_remove", str(case_id), user_id=str(case.user_id), case_action=case.action)
    return web.json_response({"ok": True})


# --- Automod tuning ---------------------------------------------------------------------------


@routes.get("/api/automod/stats")
@requires("mod.cases.view")
async def automod_stats(request: web.Request, actor: Actor) -> web.Response:
    """Per-filter hits and staff feedback, plus loosening suggestions (applied through /api/settings)."""
    days = _query_int(request, "days", 30, 365)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stats = await automod_hits.filter_stats(actor.member.guild.id, since)
    suggestions = automod_hits.suggest(stats, request.app[BOT].settings)
    return web.json_response(
        {
            "days": days,
            "filters": [
                {
                    "reason": stat.reason,
                    "hits": stat.hits,
                    "confirmed": stat.confirmed,
                    "false_positives": stat.false_positives,
                    "false_positive_rate": round(stat.false_positive_rate, 3),
                }
                for stat in stats
            ],
            "suggestions": [
                {
                    "reason": item.reason,
                    "hits": item.hits,
                    "false_positives": item.false_positives,
                    "setting": item.setting,
                    "current": item.current,
                    "suggested": item.suggested,
                    "note": item.note,
                }
                for item in suggestions
            ],
        }
    )
