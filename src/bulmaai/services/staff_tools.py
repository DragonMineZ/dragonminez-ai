"""Staff Bulma's tools (services/staff_assistant runs them): lookups that run at once, the caller's own reminders and
notes, and action tools that only create a pending row in staff_actions. The staff member confirms with a click and
execute_action then runs it. Every tool names the panel permission (web.core.PERMISSIONS) it needs; tools_for() hides
what a tier may not use and run_tool() refuses it again. Nothing here ever pings anyone."""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord

from bulmaai.services import (
    ai_usage,
    code_notes,
    knowledge,
    mod_actions,
    mod_cases,
    staff_store,
    support_info,
    tags,
    tickets,
)
from bulmaai.services.mod_actions import ModActionError
from bulmaai.web.core import PERMISSIONS, Tier, resolve_member, tier_for

log = logging.getLogger(__name__)

SOURCE = "Bulma_AI"
LIST_CAP = 15
TEXT_CAP = 400
REASON_MAX = 400
REMINDER_MAX_DAYS = 90
PURGE_MAX = 100
REPLY_MAX = 1800
NO_PINGS = discord.AllowedMentions.none()

ModelTool = dict[str, Any]


@dataclass
class ToolContext:
    bot: Any
    settings: Any
    guild: discord.Guild
    member: discord.Member
    tier: Tier
    origin: str = "discord"
    channel_id: int | None = None
    profile: dict = field(default_factory=dict)
    language: str = "en"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    permission: str
    run: Callable[[dict, ToolContext], Awaitable[dict]]
    action: bool = False

    def schema(self) -> ModelTool:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
                "additionalProperties": False,
            },
        }


class ToolError(Exception):
    """Reported to the model as {"error": message}."""


# --- small helpers -----------------------------------------------------------------------------


def _s(value: Any, limit: int = TEXT_CAP) -> str:
    return " ".join(str(value or "").split())[:limit]


def _day(when: datetime | None) -> str | None:
    return when.isoformat(timespec="minutes") if when else None


def _str(prop: str) -> dict:
    return {"type": "string", "description": prop}


def _int(prop: str) -> dict:
    return {"type": "integer", "description": prop}


_USER = _str("A member: Discord id, @mention or username.")
_TICKET = _str("A ticket: its number, or its channel id or #mention.")


async def _find_user(ctx: ToolContext, raw: Any) -> tuple[int, discord.Member | None, str]:
    """(id, member or None, display name) for an id, mention or name. ToolError when nothing matches."""
    text = _s(raw, 100).lstrip("@")
    match = re.fullmatch(r"<@!?(\d{15,22})>|(\d{15,22})", text)
    if match:
        user_id = int(match.group(1) or match.group(2))
        member = await resolve_member(ctx.guild, user_id)
        if member is not None:
            return user_id, member, member.display_name
        try:
            user = await ctx.bot.fetch_user(user_id)
        except discord.HTTPException:
            raise ToolError("No user with that id.")
        return user_id, None, user.name
    lowered = text.lower()
    member = ctx.guild.get_member_named(text) or next(
        (m for m in ctx.guild.members if lowered in (m.name.lower(), m.display_name.lower())), None
    )
    if member is None:
        raise ToolError(f"No member named {text!r} here. Ask for their id or a mention.")
    return member.id, member, member.display_name


def _label(user_id: int, name: str) -> str:
    return f"{discord.utils.escape_markdown(name)} ({user_id})"


async def _find_ticket(ctx: ToolContext, raw: Any) -> tuple[tickets.Ticket, discord.TextChannel | None]:
    from bulmaai.cogs.tickets import is_mod_only

    text = re.sub(r"[<>#]", "", _s(raw, 40))
    if not text.isdigit():
        raise ToolError("Give a ticket number or channel.")
    number = int(text)
    ticket = await tickets.get_ticket_by_channel(number) if number > 100000 else await tickets.get_ticket_by_id(number)
    if ticket is None or ticket.guild_id != ctx.guild.id:
        raise ToolError("No such ticket.")
    if is_mod_only(ticket.category) and ctx.tier < Tier.MODERATOR:
        raise ToolError("That ticket is moderators-only.")
    channel = ctx.guild.get_channel(ticket.channel_id) if ticket.channel_id else None
    return ticket, channel if isinstance(channel, discord.TextChannel) else None


def _need_open(ticket: tickets.Ticket, channel: discord.TextChannel | None) -> discord.TextChannel:
    if ticket.status != tickets.STATUS_OPEN or channel is None:
        raise ToolError("That ticket isn't open.")
    return channel


# --- reminder time -----------------------------------------------------------------------------

_RELATIVE = re.compile(r"(\d+)\s*(weeks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m)")
_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60}
_DAY_WORD = re.compile(r"(today|tomorrow)(?:\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?)?")


def _zone(name: str | None):
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return timezone.utc


def parse_when(raw: Any, timezone_name: str | None, *, now: datetime | None = None) -> datetime:
    """A reminder time as an aware UTC datetime: ISO (naive = the person's timezone), relative ("2h", "1h30m",
    "in 3 days") or "today|tomorrow [HH:MM]" (default 09:00). ValueError for anything else, the past or > 90 days."""
    text = " ".join(str(raw or "").lower().split())
    if not text:
        raise ValueError("Say when.")
    zone = _zone(timezone_name)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_now = now.astimezone(zone)
    stripped = re.sub(r"^in\s+", "", text)
    day = _DAY_WORD.fullmatch(text)
    if day:
        hour, minute = int(day.group(2) or 9), int(day.group(3) or 0)
        if hour > 23 or minute > 59:
            raise ValueError("That time of day doesn't exist.")
        base = local_now.date() + timedelta(days=1 if day.group(1) == "tomorrow" else 0)
        due = datetime(base.year, base.month, base.day, hour, minute, tzinfo=zone)
    elif re.match(r"\d+\s*[a-z]", stripped):
        parts = _RELATIVE.findall(stripped)
        if not parts or _RELATIVE.sub("", stripped).strip(" ,") not in ("", "and"):
            raise ValueError("I couldn't read that time.")
        seconds = sum(int(amount) * _UNIT_SECONDS[unit[0]] for amount, unit in parts)
        due = now + timedelta(seconds=seconds)
    else:
        try:
            due = datetime.fromisoformat(str(raw).strip())
        except ValueError:
            raise ValueError("I couldn't read that time.")
        if due.tzinfo is None:
            due = due.replace(tzinfo=zone)
    due = due.astimezone(timezone.utc)
    if due <= now:
        raise ValueError("That time is in the past.")
    if due > now + timedelta(days=REMINDER_MAX_DAYS):
        raise ValueError(f"Reminders can be at most {REMINDER_MAX_DAYS} days away.")
    return due


# --- read tools --------------------------------------------------------------------------------


async def _member_lookup(args: dict, ctx: ToolContext) -> dict:
    user_id, member, name = await _find_user(ctx, args.get("user"))
    banned = False
    try:
        await ctx.guild.fetch_ban(discord.Object(id=user_id))
        banned = True
    except discord.NotFound:
        pass
    except discord.HTTPException:
        banned = None
    warns = await mod_cases.count_active_since(ctx.guild.id, user_id, "warn", None)
    result: dict[str, Any] = {
        "id": user_id,
        "name": _s(name),
        "in_server": member is not None,
        "banned": banned,
        "active_warnings": warns,
        "account_created": _day(discord.utils.snowflake_time(user_id)),
    }
    if member is not None:
        roles = [role.name for role in sorted(member.roles, key=lambda r: -r.position) if not role.is_default()]
        result.update(
            joined=_day(member.joined_at),
            top_roles=roles[:6],
            timed_out_until=_day(member.timed_out_until) if member.timed_out_until else None,
            panel_tier=tier_for(member, ctx.settings).name.lower(),
        )
    return result


async def _case_history(args: dict, ctx: ToolContext) -> dict:
    user_id, _, name = await _find_user(ctx, args.get("user"))
    limit = max(1, min(int(args.get("limit") or 10), LIST_CAP))
    cases = await mod_cases.list_cases(ctx.guild.id, user_id=user_id, limit=limit)
    return {
        "user": _label(user_id, name),
        "cases": [
            {
                "case": case.id,
                "action": case.action,
                "reason": _s(case.reason, 200),
                "moderator_id": case.moderator_id,
                "source": case.source,
                "date": _day(case.created_at),
                "active": case.active,
            }
            for case in cases
        ],
    }


def _ticket_row(ticket: tickets.Ticket) -> dict:
    return {
        "ticket": ticket.ticket_id,
        "channel_id": ticket.channel_id,
        "owner_id": ticket.owner_id,
        "category": ticket.category,
        "status": ticket.status,
        "language": ticket.language,
        "opened": _day(ticket.created_at),
        "claimed_by": ticket.claimed_by,
    }


async def _open_tickets(args: dict, ctx: ToolContext) -> dict:
    from bulmaai.cogs.tickets import is_mod_only

    owner_id = None
    if args.get("owner"):
        owner_id = (await _find_user(ctx, args["owner"]))[0]
    rows = [
        ticket
        for ticket in await tickets.list_active_tickets()
        if ticket.status == tickets.STATUS_OPEN
        and ticket.guild_id == ctx.guild.id
        and (owner_id is None or ticket.owner_id == owner_id)
        and (ctx.tier >= Tier.MODERATOR or not is_mod_only(ticket.category))
    ]
    rows.sort(key=lambda ticket: ticket.created_at)
    return {"open_tickets": len(rows), "tickets": [_ticket_row(ticket) for ticket in rows[:LIST_CAP]]}


async def _ticket_info(args: dict, ctx: ToolContext) -> dict:
    ticket, _ = await _find_ticket(ctx, args.get("ticket"))
    info = _ticket_row(ticket)
    info.update(closed=_day(ticket.closed_at), closed_by=ticket.closed_by, close_reason=_s(ticket.close_reason, 200))
    return info


async def _ticket_summary(args: dict, ctx: ToolContext) -> dict:
    ticket, channel = await _find_ticket(ctx, args.get("ticket"))
    channel = _need_open(ticket, channel)
    cog = ctx.bot.get_cog("AITicketsCog")
    if cog is None:
        raise ToolError("The ticket AI isn't loaded.")
    summary = await cog.ticket_summary(channel, ticket)
    if summary is None:
        return {"summary": None, "note": "Nothing to summarize yet."}
    return {
        "ticket": ticket.ticket_id,
        "problem": summary.problem,
        "tried": summary.tried,
        "current_state": summary.current_state,
        "next_step": summary.next_step,
    }


async def _search_knowledge(args: dict, ctx: ToolContext) -> dict:
    found = await knowledge.search(_s(args.get("query"), 200), 5)
    return {
        "results": [
            {"title": item["title"], "url": item["url"], "text": _s(item["text"], 600)} for item in found
        ]
    }


async def _search_code_notes(args: dict, ctx: ToolContext) -> dict:
    from bulmaai.services import ai_tools

    return await ai_tools.search_code_notes(_s(args.get("query"), 200), _bot_context=ctx.bot)


async def _tag_lookup(args: dict, ctx: ToolContext) -> dict:
    category = _s(args.get("category"), 20).lower()
    if category not in tags.CATEGORIES:
        raise ToolError(f"Categories: {', '.join(tags.CATEGORIES)}.")
    tag = await tags.get(category, _s(args.get("name"), 60))
    if tag is None:
        names = await tags.names(category, limit=20)
        return {"error": "No such tag.", "tags_in_category": [name for _, name in names]}
    return {"category": tag.category, "name": tag.name, "body": _s(tag.body, 1500)}


async def _support_info_list(args: dict, ctx: ToolContext) -> dict:
    entries = await support_info.list_active()
    return {
        "entries": [
            {
                "id": entry.id,
                "kind": entry.kind,
                "title": _s(entry.title, 120),
                "body": _s(entry.body, 500),
                "expires": _day(entry.expires_at),
            }
            for entry in entries[:LIST_CAP]
        ]
    }


async def _ai_spend(args: dict, ctx: ToolContext) -> dict:
    from bulmaai.database.db import get_pool

    today = datetime.now(timezone.utc).date()
    start, end = ai_usage.cycle_bounds(today, getattr(ctx.settings, "ai_budget_cycle_day", 1))
    await ai_usage.flush()
    rows = await (await get_pool()).fetch(
        """
        SELECT feature, COALESCE(SUM(cost_usd), 0) AS cost, count(*) AS calls FROM ai_usage
        WHERE created_at >= $1 GROUP BY feature ORDER BY cost DESC LIMIT 10
        """,
        ai_usage.utc_midnight(start),
    )
    total = sum(float(row["cost"]) for row in rows)
    return {
        "cycle_start": start.isoformat(),
        "cycle_end": end.isoformat(),
        "budget_usd": float(getattr(ctx.settings, "ai_monthly_budget_usd", 0) or 0),
        "spent_usd": round(total, 2),
        "top_features": [
            {"feature": ai_usage.FEATURE_LABELS.get(row["feature"], row["feature"]), "usd": round(float(row["cost"]), 2), "calls": row["calls"]}
            for row in rows
        ],
    }


# --- the caller's own data ---------------------------------------------------------------------


async def _search_my_messages(args: dict, ctx: ToolContext) -> dict:
    rows = await staff_store.search_vault(ctx.member.id, _s(args.get("query"), 200), 8)
    return {
        "messages": [
            {"channel_id": row["channel_id"], "date": _day(row["created_at"]), "text": _s(row["content"], 500)}
            for row in rows
        ]
    }


async def _list_my_reminders(args: dict, ctx: ToolContext) -> dict:
    rows = await staff_store.list_reminders(ctx.member.id)
    return {"reminders": [{"id": row["id"], "text": row["text"], "due": _day(row["due_at"])} for row in rows[:LIST_CAP]]}


async def _add_reminder(args: dict, ctx: ToolContext) -> dict:
    text = _s(args.get("text"), staff_store.REMINDER_MAX)
    if not text:
        raise ToolError("What should the reminder say?")
    try:
        due = parse_when(args.get("when"), (ctx.profile or {}).get("timezone"))
    except ValueError as error:
        raise ToolError(str(error))
    reminder_id = await staff_store.add_reminder(ctx.member.id, text, due)
    zone = _zone((ctx.profile or {}).get("timezone"))
    return {"reminder": reminder_id, "due": due.astimezone(zone).isoformat(timespec="minutes")}


async def _remember(args: dict, ctx: ToolContext) -> dict:
    note_id = await staff_store.add_note(ctx.member.id, _s(args.get("note"), staff_store.NOTE_MAX), "user")
    if note_id is None:
        raise ToolError("Nothing to remember.")
    return {"note": note_id}


async def _forget_note(args: dict, ctx: ToolContext) -> dict:
    try:
        note_id = int(args.get("note_id"))
    except (TypeError, ValueError):
        raise ToolError("Give the note's id.")
    return {"forgotten": await staff_store.delete_note(ctx.member.id, note_id)}


# --- action tools: proposal --------------------------------------------------------------------


def _reason(args: dict) -> str:
    reason = _s(args.get("reason"), REASON_MAX)
    if not reason:
        raise ToolError("A reason is required.")
    return reason


def _duration(args: dict, *, required: bool) -> int | None:
    raw = args.get("duration")
    seconds = mod_actions.parse_duration_seconds(str(raw)) if raw else None
    if required and not seconds:
        raise ToolError("Give a duration like 1h, 2d or 30m.")
    if raw and not seconds and not required and str(raw).strip().lower() not in ("permanent", "forever"):
        raise ToolError("I couldn't read that duration.")
    return seconds


async def _propose(ctx: ToolContext, action: str, args: dict, summary: str) -> dict:
    action_id = await staff_store.create_action(ctx.member.id, ctx.guild.id, action, args, summary, ctx.origin)
    return {
        "pending_action": action_id,
        "summary": summary,
        "note": "Waiting for the staff member's confirmation click. Nothing has happened yet.",
    }


async def _propose_member_action(action: str, args: dict, ctx: ToolContext) -> dict:
    user_id, member, name = await _find_user(ctx, args.get("user"))
    if member is None and action in mod_actions.MEMBER_ONLY_ACTIONS:
        raise ToolError("That user isn't in the server.")
    mod_actions.check_hierarchy(
        ctx.bot, ctx.guild, ctx.member, user_id, member, discord_action=action in mod_actions.DISCORD_ACTIONS
    )
    stored: dict[str, Any] = {"user_id": user_id, "name": _s(name, 80)}
    who = _label(user_id, name)
    if action in ("warn", "note", "timeout", "kick", "ban", "untimeout", "unban"):
        stored["reason"] = _reason(args) if action not in ("untimeout", "unban") else _s(args.get("reason"), REASON_MAX)
    if action == "timeout":
        stored["duration_seconds"] = _duration(args, required=True)
    if action == "ban":
        stored["duration_seconds"] = _duration(args, required=False)
        stored["delete_days"] = max(0, min(int(args.get("delete_days") or 0), 7))
    verb = {"warn": "Warn", "note": "Add a note to", "timeout": "Time out", "untimeout": "Remove the timeout of",
            "kick": "Kick", "ban": "Ban", "unban": "Unban", "clearwarns": "Clear all active warnings of"}[action]
    summary = f"{verb} {who}"
    if stored.get("duration_seconds"):
        summary += f" for {mod_actions.format_duration(stored['duration_seconds'])}"
    if stored.get("delete_days"):
        summary += f", deleting {stored['delete_days']}d of messages"
    if stored.get("reason"):
        summary += f": {stored['reason']}"
    return await _propose(ctx, action, stored, summary)


def _member_action(action: str) -> Callable[[dict, ToolContext], Awaitable[dict]]:
    async def run(args: dict, ctx: ToolContext) -> dict:
        return await _propose_member_action(action, args, ctx)

    return run


async def _propose_purge(args: dict, ctx: ToolContext) -> dict:
    raw = re.sub(r"[<>#]", "", _s(args.get("channel"), 40))
    channel = ctx.guild.get_channel(int(raw)) if raw.isdigit() else discord.utils.get(ctx.guild.text_channels, name=raw.lower())
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise ToolError("I couldn't find that text channel.")
    count = max(1, min(int(args.get("count") or 0), PURGE_MAX))
    stored: dict[str, Any] = {"channel_id": channel.id, "count": count}
    summary = f"Delete up to {count} recent messages (last 14 days) in <#{channel.id}>"
    if args.get("user"):
        user_id, _, name = await _find_user(ctx, args["user"])
        stored["user_id"] = user_id
        summary += f" from {_label(user_id, name)}"
    return await _propose(ctx, "purge", stored, summary)


async def _propose_ticket_action(action: str, args: dict, ctx: ToolContext) -> dict:
    ticket, channel = await _find_ticket(ctx, args.get("ticket"))
    _need_open(ticket, channel)
    stored: dict[str, Any] = {"ticket_id": ticket.ticket_id, "channel_id": ticket.channel_id}
    number = f"ticket #{ticket.ticket_id}"
    if action == "ticket_close":
        stored["reason"] = _s(args.get("reason"), 200)
        summary = f"Close {number}" + (f": {stored['reason']}" if stored["reason"] else "")
    elif action == "ticket_solve":
        summary = f"Close {number} as solved"
    elif action == "ticket_add_member":
        user_id, member, name = await _find_user(ctx, args.get("user"))
        if member is None:
            raise ToolError("That user isn't in the server.")
        stored.update(user_id=user_id, name=_s(name, 80))
        summary = f"Add {_label(user_id, name)} to {number}"
    else:
        text = _s(args.get("text"), REPLY_MAX)
        if not text:
            raise ToolError("What should the reply say?")
        stored["text"] = text
        summary = f"Post this reply in {number}, signed with your name: {text[:300]}"
    return await _propose(ctx, action, stored, summary)


def _ticket_action(action: str) -> Callable[[dict, ToolContext], Awaitable[dict]]:
    async def run(args: dict, ctx: ToolContext) -> dict:
        return await _propose_ticket_action(action, args, ctx)

    return run


# --- the registry ------------------------------------------------------------------------------

_REASON = _str("Why (required, short).")
_TOOL_LIST = [
    Tool("member_lookup", "Look up a member: roles, join date, timeout, ban state, active warnings.", {"user": _USER}, ("user",), "users.view", _member_lookup),
    Tool("case_history", "A member's moderation cases, newest first.", {"user": _USER, "limit": _int("How many, up to 15.")}, ("user",), "mod.cases.view", _case_history),
    Tool("open_tickets", "List open tickets, optionally only one owner's.", {"owner": _USER}, (), "tickets.view", _open_tickets),
    Tool("ticket_info", "Details of one ticket (owner, category, status, dates).", {"ticket": _TICKET}, ("ticket",), "tickets.view", _ticket_info),
    Tool("ticket_summary", "AI summary of an open ticket so far (problem, tried, state, next step).", {"ticket": _TICKET}, ("ticket",), "tickets.view", _ticket_summary),
    Tool("search_knowledge", "Search the wiki, solved tickets and FAQ. Query in English keywords.", {"query": _str("Keywords.")}, ("query",), "status.view", _search_knowledge),
    Tool("search_code_notes", "Search private notes on what recent pushes to the mod repo changed.", {"query": _str("Keywords.")}, ("query",), "status.view", _search_code_notes),
    Tool("tag_lookup", "Read a canned tag answer.", {"category": _str("api, error, mixin or other."), "name": _str("Tag name.")}, ("category", "name"), "status.view", _tag_lookup),
    Tool("support_info_list", "The active support info entries the support AI is told about.", {}, (), "status.view", _support_info_list),
    Tool("ai_spend", "This budget cycle's AI spend against the monthly budget.", {}, (), "status.view", _ai_spend),
    Tool("search_my_messages", "Search the caller's own past messages (their private vault). Never anyone else's.", {"query": _str("Keywords.")}, ("query",), "status.view", _search_my_messages),
    Tool("list_my_reminders", "The caller's pending reminders.", {}, (), "status.view", _list_my_reminders),
    Tool("add_reminder", "Remind the caller later by DM. when: ISO datetime, '2h', '30m', '1h30m' or 'tomorrow 9:00' (their timezone). Not past, max 90 days.", {"text": _str("What to remind."), "when": _str("When.")}, ("text", "when"), "status.view", _add_reminder),
    Tool("remember", "Save a durable note about the caller (preference, habit, ongoing situation).", {"note": _str("Short note.")}, ("note",), "status.view", _remember),
    Tool("forget_note", "Delete one of the caller's memory notes by id.", {"note_id": _int("Note id.")}, ("note_id",), "status.view", _forget_note),
    Tool("warn", "Propose warning a member. Waits for the caller's confirmation click.", {"user": _USER, "reason": _REASON}, ("user", "reason"), "mod.warn", _member_action("warn"), True),
    Tool("note", "Propose a private staff note on a member. Waits for confirmation.", {"user": _USER, "reason": _str("The note.")}, ("user", "reason"), "mod.warn", _member_action("note"), True),
    Tool("timeout", "Propose timing a member out. Waits for confirmation.", {"user": _USER, "duration": _str("e.g. 1h, 2d (max 28d)."), "reason": _REASON}, ("user", "duration", "reason"), "mod.timeout", _member_action("timeout"), True),
    Tool("untimeout", "Propose removing a member's timeout. Waits for confirmation.", {"user": _USER, "reason": _str("Optional.")}, ("user",), "mod.timeout", _member_action("untimeout"), True),
    Tool("kick", "Propose kicking a member. Waits for confirmation.", {"user": _USER, "reason": _REASON}, ("user", "reason"), "mod.kick", _member_action("kick"), True),
    Tool("ban", "Propose banning a user (duration makes it temporary). Waits for confirmation.", {"user": _USER, "reason": _REASON, "duration": _str("Optional, e.g. 7d."), "delete_days": _int("Days of messages to delete, 0-7.")}, ("user", "reason"), "mod.ban", _member_action("ban"), True),
    Tool("unban", "Propose unbanning a user. Waits for confirmation.", {"user": _str("User id."), "reason": _str("Optional.")}, ("user",), "mod.ban", _member_action("unban"), True),
    Tool("clearwarns", "Propose clearing all of a member's active warnings. Waits for confirmation.", {"user": _USER}, ("user",), "mod.cases.remove", _member_action("clearwarns"), True),
    Tool("purge", "Propose bulk-deleting recent messages (max 100, last 14 days) in a channel. Waits for confirmation.", {"channel": _str("Channel id or #mention."), "count": _int("1-100."), "user": _str("Only this user's messages (optional).")}, ("channel", "count"), "mod.channels", _propose_purge, True),
    Tool("ticket_close", "Propose closing an open ticket. Waits for confirmation.", {"ticket": _TICKET, "reason": _str("Optional.")}, ("ticket",), "tickets.view", _ticket_action("ticket_close"), True),
    Tool("ticket_solve", "Propose closing an open ticket as solved. Waits for confirmation.", {"ticket": _TICKET}, ("ticket",), "tickets.view", _ticket_action("ticket_solve"), True),
    Tool("ticket_add_member", "Propose giving a member access to an open ticket. Waits for confirmation.", {"ticket": _TICKET, "user": _USER}, ("ticket", "user"), "tickets.view", _ticket_action("ticket_add_member"), True),
    Tool("ticket_reply", "Propose a public reply in an open ticket, signed with the caller's name. Waits for confirmation.", {"ticket": _TICKET, "text": _str("The reply, in the ticket's language.")}, ("ticket", "text"), "tickets.view", _ticket_action("ticket_reply"), True),
]
TOOLS: dict[str, Tool] = {tool.name: tool for tool in _TOOL_LIST}
ACTION_TOOLS = frozenset(tool.name for tool in _TOOL_LIST if tool.action)

# Permission that executing a stored action needs (the same as proposing it).
ACTION_PERMISSION = {tool.name: tool.permission for tool in _TOOL_LIST if tool.action}


def tools_for(tier: Tier) -> list[ModelTool]:
    return [tool.schema() for tool in _TOOL_LIST if tier >= PERMISSIONS[tool.permission]]


async def run_tool(name: str, args: dict, ctx: ToolContext) -> dict:
    """Never raises: a refusal or failure comes back as {"error": ...}."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Unknown tool {name}."}
    if ctx.tier < PERMISSIONS[tool.permission]:
        return {"error": f"Your role can't use {name}."}
    try:
        return await tool.run(args if isinstance(args, dict) else {}, ctx)
    except ToolError as error:
        return {"error": str(error)}
    except ModActionError as error:
        return {"error": error.text("en")}
    except Exception:
        log.exception("Staff tool %s failed", name)
        return {"error": f"{name} failed; try again or say it didn't work."}


# --- executing a confirmed action --------------------------------------------------------------


async def execute_action(row: dict, bot, member: discord.Member) -> str:
    """Runs a claimed staff_actions row after the confirm click. Tier and hierarchy are checked again now. Raises
    ModActionError or ValueError(message) on a refusal or failure; returns a short result text."""
    action = row["action"]
    args = row["args"]
    permission = ACTION_PERMISSION.get(action)
    guild = bot.get_guild(row["guild_id"])
    if permission is None or guild is None:
        raise ValueError("That action isn't available any more.")
    if member.guild.id != guild.id or tier_for(member, bot.settings) < PERMISSIONS[permission]:
        raise ValueError("Your role no longer allows that.")
    if action in mod_actions.DISCORD_ACTIONS | {"warn", "note"}:
        return await _execute_mod(action, args, bot, guild, member)
    if action == "clearwarns":
        return await _execute_clearwarns(args, bot, guild, member)
    if action == "purge":
        return await _execute_purge(args, bot, guild, member)
    return await _execute_ticket(action, args, bot, guild, member)


async def _execute_mod(action: str, args: dict, bot, guild: discord.Guild, member: discord.Member) -> str:
    user_id = int(args["user_id"])
    result = await mod_actions.perform(
        bot,
        guild,
        action=action,
        target_id=user_id,
        moderator=member,
        reason=args.get("reason") or "",
        duration_seconds=args.get("duration_seconds"),
        delete_message_seconds=int(args.get("delete_days") or 0) * 86400,
        source=SOURCE,
    )
    text = f"Done: {action} on {discord.utils.escape_markdown(args.get('name') or str(user_id))} ({user_id})."
    if result.case_id:
        text += f" Case #{result.case_id}."
    if getattr(result, "ladder_skipped", None) or getattr(result, "escalation", None):
        text += " The warn ladder escalated."
    return text


async def _execute_clearwarns(args: dict, bot, guild: discord.Guild, member: discord.Member) -> str:
    user_id = int(args["user_id"])
    target = await resolve_member(guild, user_id)
    mod_actions.check_hierarchy(bot, guild, member, user_id, target, discord_action=False)
    count = await mod_actions.end_user_cases(bot, guild.id, user_id, "warn", ended_by=member.id)
    text = f"Cleared {count} warning(s) for <@{user_id}>."
    if (cog := bot.get_cog("ModCommandsCog")) is not None:
        await cog._log_removal("Warnings cleared", f"{text} (via {SOURCE})", member.id, user_id)
    return text


async def _execute_purge(args: dict, bot, guild: discord.Guild, member: discord.Member) -> str:
    from bulmaai.cogs.mod_commands import purge_check

    channel = guild.get_channel_or_thread(int(args["channel_id"]))
    if channel is None:
        raise ValueError("That channel no longer exists.")
    user_id = args.get("user_id")
    try:
        deleted = await channel.purge(
            limit=min(int(args["count"]), PURGE_MAX),
            check=purge_check(user_id=int(user_id) if user_id else None),
            after=discord.utils.utcnow() - timedelta(days=14),
            reason=f"purge by {member.name} (via {SOURCE})",
        )
    except discord.HTTPException:
        raise ValueError("Discord refused: the bot can't delete messages there.")
    if deleted and (cog := bot.get_cog("ModCommandsCog")) is not None:
        text = f"**Channel** {channel.mention}　**Moderator** {member.mention}　**Deleted** {len(deleted)}\n(via {SOURCE})"
        await cog._post_mod_log("purge", "🧹 Purge", text, user_id=member.id, data={"channel_id": channel.id})
    return f"Deleted {len(deleted)} message(s) in <#{channel.id}>."


async def _execute_ticket(action: str, args: dict, bot, guild: discord.Guild, member: discord.Member) -> str:
    from bulmaai.cogs.tickets import is_mod_only

    ticket = await tickets.get_ticket_by_id(int(args["ticket_id"]))
    channel = guild.get_channel(ticket.channel_id) if ticket and ticket.channel_id else None
    if ticket is None or ticket.status != tickets.STATUS_OPEN or not isinstance(channel, discord.TextChannel):
        raise ValueError("That ticket isn't open any more.")
    if is_mod_only(ticket.category) and tier_for(member, bot.settings) < Tier.MODERATOR:
        raise ValueError("That ticket is moderators-only.")
    cog = bot.get_cog("TicketsCog")
    if cog is None:
        raise ValueError("Tickets aren't loaded.")
    number = f"ticket #{ticket.ticket_id}"
    if action == "ticket_close":
        done = await cog.close_ticket(channel, closer_id=member.id, reason=args.get("reason") or None)
        if done is None:
            raise ValueError(f"{number} was already closed.")
        return f"Closed {number}."
    if action == "ticket_solve":
        done = await cog.solve_ticket(channel, closer_id=member.id, staff_solved=True)
        if done is None:
            raise ValueError(f"{number} was already closed.")
        return f"Closed {number} as solved."
    if action == "ticket_add_member":
        target = await resolve_member(guild, int(args["user_id"]))
        if target is None:
            raise ValueError("They aren't in the server any more.")
        await cog.add_to_ticket(channel, target, f"Bulma_AI: added by {member}")
        return f"Added {discord.utils.escape_markdown(target.display_name)} to {number}."
    if action == "ticket_reply":
        if not await cog._post_staff_reply(channel, member, args["text"]):
            raise ValueError("Discord refused the reply.")
        return f"Posted your reply in {number}."
    raise ValueError("That action isn't available any more.")
