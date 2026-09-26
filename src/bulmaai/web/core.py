"""Shared admin-panel plumbing: sessions, staff tiers, permission checks, audit log."""

import hashlib
import hmac
import json
import logging
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from functools import wraps
from typing import Any

import discord
from aiohttp import web

from bulmaai.database.db import get_pool
from bulmaai.utils.permissions import is_bruno


log = logging.getLogger(__name__)

BOT = web.AppKey("bot", discord.Bot)
SESSION_COOKIE = "panel_session"
SESSION_TTL_SECONDS = 12 * 3600


class Tier(IntEnum):
    NONE = 0
    HELPER = 1
    MODERATOR = 2
    ADMIN = 3
    OWNER = 4


# Minimum tier per capability. The frontend hides what /api/me doesn't list; every route re-checks.
PERMISSIONS: dict[str, Tier] = {
    "status.view": Tier.HELPER,
    "tickets.view": Tier.HELPER,
    "users.view": Tier.HELPER,
    "mod.cases.view": Tier.HELPER,
    "mod.warn": Tier.HELPER,
    "mod.timeout": Tier.MODERATOR,
    "mod.kick": Tier.MODERATOR,
    "logs.view": Tier.MODERATOR,
    "tickets.manage": Tier.MODERATOR,
    "patreon.view": Tier.MODERATOR,
    "mod.ban": Tier.ADMIN,
    "settings.view": Tier.ADMIN,
    "settings.edit": Tier.ADMIN,
    "patreon.manage": Tier.ADMIN,
    "presets.edit": Tier.ADMIN,
    "announce.send": Tier.ADMIN,
    "bot.reload": Tier.ADMIN,
    "audit.view": Tier.ADMIN,
    "settings.edit_panel": Tier.OWNER,
}


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def sign_session(secret: str, user_id: int, *, now: float | None = None) -> str:
    expires_at = int((now or time.time()) + SESSION_TTL_SECONDS)
    raw = json.dumps({"uid": str(user_id), "exp": expires_at}, separators=(",", ":")).encode()
    body = urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{body}.{_sign(secret, body)}"


def read_session(secret: str, value: str | None, *, now: float | None = None) -> int | None:
    if not value or "." not in value:
        return None
    body, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(secret, body)):
        return None
    try:
        payload = json.loads(urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if int(payload["exp"]) < (now or time.time()):
            return None
        return int(payload["uid"])
    except (KeyError, TypeError, ValueError):
        return None


def panel_guild(bot: discord.Bot) -> discord.Guild | None:
    guild_id = bot.settings.panel_guild_id
    if guild_id:
        return bot.get_guild(guild_id)
    return bot.guilds[0] if bot.guilds else None


def tier_for(member: discord.Member, settings: Any) -> Tier:
    if is_bruno(member) or member.id == member.guild.owner_id:
        return Tier.OWNER
    if member.guild_permissions.administrator:
        return Tier.ADMIN
    role_ids = {role.id for role in member.roles}
    for tier, configured in (
        (Tier.ADMIN, settings.panel_admin_role_ids),
        (Tier.MODERATOR, settings.panel_moderator_role_ids),
        (Tier.HELPER, settings.panel_helper_role_ids),
    ):
        if role_ids & set(configured):
            return tier
    return Tier.NONE


@dataclass(frozen=True)
class Actor:
    member: discord.Member
    tier: Tier

    @property
    def id(self) -> int:
        return self.member.id

    def can(self, permission: str) -> bool:
        return self.tier >= PERMISSIONS[permission]

    @property
    def permissions(self) -> list[str]:
        return sorted(name for name, tier in PERMISSIONS.items() if self.tier >= tier)


async def resolve_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def get_actor(request: web.Request) -> Actor | None:
    """Re-resolved on every request so removing someone's staff role revokes access immediately."""
    bot = request.app[BOT]
    user_id = read_session(bot.settings.panel_session_secret or "", request.cookies.get(SESSION_COOKIE))
    guild = panel_guild(bot)
    if user_id is not None and guild is not None:
        member = await resolve_member(guild, user_id)
        if member is not None:
            tier = tier_for(member, bot.settings)
            if tier > Tier.NONE:
                return Actor(member=member, tier=tier)
    return None


Handler = Callable[[web.Request, Actor], Awaitable[web.StreamResponse]]


def requires(permission: str) -> Callable[[Handler], Callable[[web.Request], Awaitable[web.StreamResponse]]]:
    if permission not in PERMISSIONS:
        raise KeyError(permission)

    def decorator(handler: Handler):
        @wraps(handler)
        async def wrapper(request: web.Request) -> web.StreamResponse:
            actor = await get_actor(request)
            if actor is None:
                raise api_error(401, "Not logged in.")
            if not actor.can(permission):
                raise api_error(403, "Your staff tier can't do that.")
            return await handler(request, actor)

        return wrapper

    return decorator


def api_error(status: int, message: str) -> web.HTTPException:
    """Build a JSON error; callers write `raise api_error(...)`."""
    return _ERRORS[status](text=json.dumps({"error": message}), content_type="application/json")


_ERRORS: dict[int, type[web.HTTPException]] = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
    404: web.HTTPNotFound,
    409: web.HTTPConflict,
    502: web.HTTPBadGateway,
    503: web.HTTPServiceUnavailable,
}


async def read_json(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise api_error(400, "Body must be JSON.")
    if not isinstance(payload, dict):
        raise api_error(400, "Body must be a JSON object.")
    return payload


def user_json(user: discord.abc.User | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {
        "id": str(user.id),
        "name": user.name,
        "display_name": getattr(user, "display_name", user.name),
        "avatar": user.display_avatar.url,
        "bot": user.bot,
    }


def require_guild(request: web.Request) -> discord.Guild:
    guild = panel_guild(request.app[BOT])
    if guild is None:
        raise api_error(503, "Bot is not connected to the guild yet.")
    return guild


async def audit(actor: Actor, action: str, target: str | None = None, **details: Any) -> None:
    """Record a panel action. Call after the action succeeds; a failed insert never undoes it."""
    log.info("panel: %s (%s) %s %s %s", actor.member, actor.id, action, target or "", details or "")
    try:
        pool = await get_pool()
        await pool.execute(
            "INSERT INTO panel_audit_log (actor_id, action, target, details) VALUES ($1, $2, $3, $4::jsonb)",
            actor.id,
            action,
            target,
            json.dumps(details, default=str),
        )
    except Exception:
        log.exception("Failed to write panel audit log entry for %s", action)
