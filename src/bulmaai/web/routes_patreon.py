"""Patreon links and beta whitelist grants. Grant/revoke go through the whitelist flow cog."""

import logging
from typing import Any

import discord
from aiohttp import web

from bulmaai.cogs.patreon_whitelist_flow import _active_self_grant
from bulmaai.database.db import get_pool
from bulmaai.services.patreon_grants import (
    PatreonGrantKind,
    deactivate_gift_grant,
    deactivate_grants_for_owner,
    list_active_grants_for_owner,
)
from bulmaai.ui.patreon_views import MC_NAME_RE
from bulmaai.web.core import BOT, Actor, api_error, audit, read_json, require_guild, requires, resolve_member, user_json


log = logging.getLogger(__name__)
routes = web.RouteTableDef()

PAGE_SIZE = 25
FLOW_COG = "PatreonWhitelistFlowCog"


def _page(request: web.Request) -> int:
    try:
        page = int(request.query.get("page", "1"))
    except ValueError:
        raise api_error(400, "page must be an integer.")
    if page < 1:
        raise api_error(400, "page must be at least 1.")
    return page


def _snowflake(value: Any, name: str) -> int:
    text = str(value if value is not None else "").strip()
    if not text.isdigit() or not 0 < int(text) < 2**63:
        raise api_error(400, f"{name} must be a Discord user ID.")
    return int(text)


def _person(guild: discord.Guild, user_id: int) -> dict[str, Any] | str:
    member = guild.get_member(user_id)
    return user_json(member) if member else str(user_id)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _flow_cog(request: web.Request) -> Any:
    cog = request.app[BOT].get_cog(FLOW_COG)
    if cog is None:
        raise api_error(503, "The Patreon whitelist cog isn't loaded.")
    return cog


async def _search(
    request: web.Request, *, table: str, columns: str, id_filter: str, text_filter: str, active_column: str, order: str
) -> tuple[list[Any], int, bool]:
    """Constant SQL fragments from this module only; the user's query is always a bound parameter."""
    page = _page(request)
    q = request.query.get("q", "").strip()[:100]
    args: list[Any] = []
    conditions: list[str] = []
    if q.isdigit() and int(q) < 2**63:
        args.append(int(q))
        conditions.append(id_filter)
    elif q:
        args.append(q)
        conditions.append(text_filter)
    if request.query.get("active") == "1":
        conditions.append(f"{active_column} = TRUE")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    args.extend([PAGE_SIZE + 1, (page - 1) * PAGE_SIZE])
    pool = await get_pool()
    rows = await pool.fetch(
        f"SELECT {columns} FROM {table} {where} ORDER BY {order} LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return rows[:PAGE_SIZE], page, len(rows) > PAGE_SIZE


@routes.get("/api/patreon/links")
@requires("patreon.view")
async def list_links(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    # Only identity/entitlement columns; nothing token-like is stored here, and patreon ids stay server-side.
    rows, page, has_more = await _search(
        request,
        table="patreon_links",
        columns="discord_user_id, discord_username, patreon_full_name, patron_status, tier_ids, "
        "last_charge_date, entitlement_active, linked_at, updated_at",
        id_filter="discord_user_id = $1",
        text_filter="(discord_username ILIKE '%' || $1 || '%' OR patreon_full_name ILIKE '%' || $1 || '%')",
        active_column="entitlement_active",
        order="updated_at DESC, discord_user_id",
    )
    links = [
        {
            "discord_user_id": str(row["discord_user_id"]),
            "user": _person(guild, row["discord_user_id"]),
            "discord_username": row["discord_username"],
            "patreon_full_name": row["patreon_full_name"],
            "patron_status": row["patron_status"],
            "tier_ids": list(row["tier_ids"] or []),
            "last_charge_date": _iso(row["last_charge_date"]),
            "entitlement_active": row["entitlement_active"],
            "linked_at": _iso(row["linked_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]
    return web.json_response({"links": links, "page": page, "has_more": has_more})


@routes.get("/api/patreon/grants")
@requires("patreon.view")
async def list_grants(request: web.Request, actor: Actor) -> web.Response:
    guild = require_guild(request)
    rows, page, has_more = await _search(
        request,
        table="patreon_whitelist_grants",
        columns="id, owner_discord_user_id, beneficiary_discord_user_id, beneficiary_discord_username, "
        "minecraft_username, kind, active, source_pr_url, created_at, updated_at",
        id_filter="(owner_discord_user_id = $1 OR beneficiary_discord_user_id = $1)",
        text_filter="(minecraft_username ILIKE '%' || $1 || '%' OR beneficiary_discord_username ILIKE '%' || $1 || '%')",
        active_column="active",
        order="updated_at DESC, id DESC",
    )
    grants = [
        {
            "id": row["id"],
            "owner_id": str(row["owner_discord_user_id"]),
            "owner": _person(guild, row["owner_discord_user_id"]),
            "beneficiary_id": str(row["beneficiary_discord_user_id"]),
            "beneficiary": _person(guild, row["beneficiary_discord_user_id"]),
            "beneficiary_username": row["beneficiary_discord_username"],
            "minecraft_username": row["minecraft_username"],
            "kind": row["kind"],
            "active": row["active"],
            "source_pr_url": row["source_pr_url"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]
    return web.json_response({"grants": grants, "page": page, "has_more": has_more})


@routes.post("/api/patreon/grant")
@requires("patreon.manage")
async def grant_access(request: web.Request, actor: Actor) -> web.Response:
    """Staff override of /beta-access: same whitelist PR + auto-merge + grant row, minus the Patreon checks."""
    guild = require_guild(request)
    cog = _flow_cog(request)
    payload = await read_json(request)
    user_id = _snowflake(payload.get("user_id"), "user_id")
    nickname = str(payload.get("minecraft_username") or "").strip()
    if not MC_NAME_RE.match(nickname):
        raise api_error(400, "Minecraft username must be 3-16 letters, numbers, or underscores.")
    member = await resolve_member(guild, user_id)
    if member is None:
        raise api_error(404, "That user isn't in the server.")
    if member.bot:
        raise api_error(400, "Bots can't get beta access.")

    async with cog._beta_access_lock(member.id):
        existing = _active_self_grant(await list_active_grants_for_owner(member.id), member.id)
        if existing is not None:
            raise api_error(409, f"Already whitelisted as {existing.minecraft_username}. Revoke it first.")
        try:
            approval = await cog._auto_approve_beta_access(member, nickname)
        except Exception:
            log.exception("Admin panel beta access grant failed for %s (%s)", member.id, nickname)
            raise api_error(502, "The GitHub whitelist update failed, check the bot logs.")
        if approval.pr_url is None:
            raise api_error(409, f"{nickname} is already in the whitelist file.")
        if approval.approved:
            await cog._record_self_grant(member, nickname, approval.pr_url)

    await audit(
        actor, "patreon.grant", str(member.id), minecraft_username=nickname, pr_url=approval.pr_url, merged=approval.approved
    )
    if approval.approved:
        try:
            await cog._log_staff_info(
                f"<@{actor.id}> granted beta access to <@{member.id}> as `{nickname}` from the admin panel.\n"
                f"PR: {approval.pr_url}"
            )
        except Exception:
            log.exception("Failed to post admin panel beta grant to the Patreon staff channel")
    return web.json_response({"ok": True, "merged": approval.approved, "pr_url": approval.pr_url})


@routes.post("/api/patreon/revoke")
@requires("patreon.manage")
async def revoke_access(request: web.Request, actor: Actor) -> web.Response:
    """Without beneficiary_id (or with the owner's own id): every active grant the owner holds, like a lapsed
    pledge in the Patreon webhook. With a gift recipient's id: only that gift."""
    cog = _flow_cog(request)
    payload = await read_json(request)
    owner_id = _snowflake(payload.get("owner_id"), "owner_id")
    beneficiary_id = payload.get("beneficiary_id")
    beneficiary_id = _snowflake(beneficiary_id, "beneficiary_id") if beneficiary_id not in (None, "") else None
    gift_only = beneficiary_id is not None and beneficiary_id != owner_id

    async with cog._beta_access_lock(owner_id):
        grants = await list_active_grants_for_owner(owner_id)
        if gift_only:
            grants = [
                g for g in grants if g.kind == PatreonGrantKind.GIFT and g.beneficiary_discord_user_id == beneficiary_id
            ]
        if not grants:
            raise api_error(404, "No active grants to revoke.")
        if gift_only:
            await deactivate_gift_grant(owner_id, beneficiary_id)
        else:
            grants = await deactivate_grants_for_owner(owner_id)
        github_ok = True
        try:
            await cog._remove_whitelist_grants(owner_id, grants, f"revoked by {actor.member} from the admin panel")
        except Exception:
            log.exception("Admin panel whitelist removal failed for owner %s", owner_id)
            github_ok = False

    usernames = sorted({g.minecraft_username for g in grants})
    await audit(
        actor,
        "patreon.revoke",
        str(owner_id),
        beneficiary_id=str(beneficiary_id) if gift_only else None,
        minecraft_usernames=usernames,
        github_ok=github_ok,
    )
    if not github_ok:
        raise api_error(502, "Grants were marked inactive, but the GitHub whitelist update failed. Check the bot logs.")
    return web.json_response({"ok": True, "revoked": usernames})
