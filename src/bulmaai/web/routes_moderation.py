"""Users page (lookup + profile), moderation actions and the mod case log."""

import asyncio
import logging
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from aiohttp import web

from bulmaai.database.db import get_pool
from bulmaai.services import mod_cases
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
MEMBER_ONLY_ACTIONS = {"warn", "timeout", "untimeout", "kick"}
PANEL_ONLY_ACTIONS = {"warn", "note"}  # no Discord permission involved, so the bot's role doesn't matter
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


def check_hierarchy(
    bot: discord.Bot, actor: Actor, target_id: int, target: discord.Member | None, *, discord_action: bool
) -> None:
    """Refuse actions staff shouldn't take against this target. Non-members only get the identity checks."""
    guild = actor.member.guild
    if target_id == actor.id:
        raise api_error(403, "You can't moderate yourself.")
    if target_id == guild.owner_id:
        raise api_error(403, "You can't moderate the server owner.")
    if bot.user is not None and target_id == bot.user.id:
        raise api_error(403, "You can't moderate the bot.")
    if target is None:
        return
    if tier_for(target, bot.settings) >= actor.tier:
        raise api_error(403, "That user's panel tier is equal to or above yours.")
    if actor.id != guild.owner_id and target.top_role.position >= actor.member.top_role.position:
        raise api_error(403, "That user's top role is equal to or above yours.")
    if discord_action and target.top_role.position >= guild.me.top_role.position:
        raise api_error(409, "The bot's top role isn't above that user's, so Discord won't allow it.")


def _whole_number(body: dict[str, Any], name: str, default: int | None = None) -> int:
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise api_error(400, f"{name} must be a whole number.")
    return value


async def _dm_warning(member: discord.Member, reason: str) -> bool:
    try:
        await member.send(
            f"You received a warning from the **{member.guild.name}** staff: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        return False
    return True


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

    member = await resolve_member(guild, user_id)
    if member is None and action in MEMBER_ONLY_ACTIONS:
        raise api_error(404, "That user isn't in the server.")
    check_hierarchy(bot, actor, user_id, member, discord_action=action not in PANEL_ONLY_ACTIONS)

    audit_reason = f"{reason or 'No reason given'} (via panel by {actor.member.name})"
    details: dict[str, Any] = {"reason": reason}
    duration_seconds = None
    try:
        if action == "warn":
            details["dm_sent"] = await _dm_warning(member, reason)
        elif action == "timeout":
            minutes = _whole_number(body, "minutes")
            if minutes < 1:
                raise api_error(400, "minutes must be at least 1.")
            minutes = min(minutes, MAX_TIMEOUT_MINUTES)
            duration_seconds = details["duration_seconds"] = minutes * 60
            await member.timeout_for(timedelta(minutes=minutes), reason=audit_reason)
        elif action == "untimeout":
            await member.remove_timeout(reason=audit_reason)
        elif action == "kick":
            await member.kick(reason=audit_reason)
        elif action == "ban":
            hours = _whole_number(body, "delete_message_hours", 0)
            if not 0 <= hours <= MAX_BAN_DELETE_HOURS:
                raise api_error(400, f"delete_message_hours must be 0-{MAX_BAN_DELETE_HOURS}.")
            details["delete_message_hours"] = hours
            await guild.ban(discord.Object(id=user_id), delete_message_seconds=hours * 3600, reason=audit_reason)
        elif action == "unban":
            await guild.unban(discord.Object(id=user_id), reason=audit_reason)
    except discord.NotFound:
        raise api_error(404, "That user isn't banned." if action == "unban" else "Discord couldn't find that user.")
    except discord.Forbidden:
        raise api_error(409, "Discord refused: the bot is missing permissions for that.")
    except discord.HTTPException:
        log.exception("Panel %s on %s failed", action, user_id)
        raise api_error(502, "Discord returned an error, try again.")

    try:
        case_id = await mod_cases.record_case(
            guild_id=guild.id,
            user_id=user_id,
            moderator_id=actor.id,
            action=action,
            reason=reason or None,
            duration_seconds=duration_seconds,
        )
    except Exception:
        log.exception("Failed to record mod case %s for %s", action, user_id)
        if action == "note":
            raise api_error(503, "Couldn't save the note, the database is unavailable.")
        case_id = None
    await audit(actor, f"mod.{action}", str(user_id), case_id=case_id, **details)
    return web.json_response({"ok": True, "case_id": case_id, "dm_sent": details.get("dm_sent")})


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


@routes.post("/api/users/{user_id}/unban")
@requires("mod.ban")
async def unban_user(request: web.Request, actor: Actor) -> web.Response:
    return await _moderate(request, actor, "unban")
