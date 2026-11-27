"""Every campaign member with a Discord account connected in Patreon, synced from the creator API.

The Patreon bot gives roles off the same connection; this lets Bulma, staff and the ticket panel's Patreon check
look anyone up by Discord id, linked with /patreon link or not."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bulmaai.database.db import get_pool
from bulmaai.services.http import request
from bulmaai.services.patreon_access import PATREON_API_BASE, _parse_datetime, _relationship_ids

log = logging.getLogger(__name__)

PAGE_SIZE = 1000
MAX_PAGES = 100  # ponytail: 100k members; raise it if the campaign ever gets that big


@dataclass(frozen=True, slots=True)
class PatreonMember:
    member_id: str
    discord_user_id: int
    patron_status: str | None
    tier_ids: tuple[str, ...]
    tier_titles: tuple[str, ...]
    last_charge_date: datetime | None
    last_charge_status: str | None
    next_charge_date: datetime | None


def _discord_id(user: Mapping[str, Any] | None) -> int | None:
    connections = ((user or {}).get("attributes") or {}).get("social_connections") or {}
    raw = (connections.get("discord") or {}).get("user_id")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def parse_members_page(payload: Mapping[str, Any]) -> list[PatreonMember]:
    """Members of one /campaigns/{id}/members page that have Discord connected and ever pledged."""
    included = payload.get("included") or []
    users = {str(item.get("id")): item for item in included if isinstance(item, dict) and item.get("type") == "user"}
    titles = {
        str(item.get("id")): (item.get("attributes") or {}).get("title") or ""
        for item in included
        if isinstance(item, dict) and item.get("type") == "tier"
    }
    members = []
    for data in payload.get("data") or []:
        if not isinstance(data, dict) or data.get("type") != "member":
            continue
        attrs = data.get("attributes") or {}
        user_ids = _relationship_ids(data, "user")
        discord_id = _discord_id(users.get(user_ids[0]) if user_ids else None)
        if discord_id is None or not attrs.get("patron_status"):
            continue  # no Discord connected, or a free follower who never pledged
        tier_ids = _relationship_ids(data, "currently_entitled_tiers")
        members.append(
            PatreonMember(
                member_id=str(data["id"]),
                discord_user_id=discord_id,
                patron_status=attrs.get("patron_status"),
                tier_ids=tier_ids,
                tier_titles=tuple(titles[tier] for tier in tier_ids if titles.get(tier)),
                last_charge_date=_parse_datetime(attrs.get("last_charge_date")),
                last_charge_status=attrs.get("last_charge_status"),
                next_charge_date=_parse_datetime(attrs.get("next_charge_date")),
            )
        )
    return members


async def fetch_all_members(*, creator_token: str, campaign_id: str) -> list[PatreonMember]:
    """Every page; raises on any failure so a partial list never replaces the stored one."""
    url: str | None = f"{PATREON_API_BASE}/campaigns/{campaign_id}/members"
    params: dict[str, str] | None = {
        "include": "user,currently_entitled_tiers",
        "fields[member]": "patron_status,last_charge_date,last_charge_status,next_charge_date",
        "fields[user]": "social_connections",
        "fields[tier]": "title",
        "page[count]": str(PAGE_SIZE),
    }
    members: list[PatreonMember] = []
    for _ in range(MAX_PAGES):
        response = await request("GET", url, headers={"Authorization": f"Bearer {creator_token}"}, params=params)
        response.raise_for_status()
        payload = response.json()
        members.extend(parse_members_page(payload))
        url = ((payload.get("links") or {}).get("next")) or None
        params = None  # the next link carries the query and cursor
        if url is None:
            return members
    raise RuntimeError(f"Patreon member list has more than {MAX_PAGES} pages")


async def replace_all(members: list[PatreonMember]) -> None:
    """Store a full sync: upsert every member, drop the ones Patreon no longer lists."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            """
            INSERT INTO patreon_members (member_id, discord_user_id, patron_status, tier_ids, tier_titles,
                                         last_charge_date, last_charge_status, next_charge_date, synced_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
            ON CONFLICT (member_id) DO UPDATE SET
                discord_user_id = EXCLUDED.discord_user_id, patron_status = EXCLUDED.patron_status,
                tier_ids = EXCLUDED.tier_ids, tier_titles = EXCLUDED.tier_titles,
                last_charge_date = EXCLUDED.last_charge_date, last_charge_status = EXCLUDED.last_charge_status,
                next_charge_date = EXCLUDED.next_charge_date, synced_at = now()
            """,
            [
                (
                    m.member_id, m.discord_user_id, m.patron_status, list(m.tier_ids), list(m.tier_titles),
                    m.last_charge_date, m.last_charge_status, m.next_charge_date,
                )
                for m in members
            ],
        )
        await conn.execute(
            "DELETE FROM patreon_members WHERE NOT (member_id = ANY($1::text[]))", [m.member_id for m in members]
        )


async def get_member_by_discord(discord_user_id: int) -> PatreonMember | None:
    """The member connected to this Discord account; an active patron wins over an old membership."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM patreon_members WHERE discord_user_id = $1
            ORDER BY (patron_status = 'active_patron') DESC, last_charge_date DESC NULLS LAST LIMIT 1
            """,
            discord_user_id,
        )
    if row is None:
        return None
    return PatreonMember(
        member_id=row["member_id"],
        discord_user_id=row["discord_user_id"],
        patron_status=row["patron_status"],
        tier_ids=tuple(row["tier_ids"] or ()),
        tier_titles=tuple(row["tier_titles"] or ()),
        last_charge_date=row["last_charge_date"],
        last_charge_status=row["last_charge_status"],
        next_charge_date=row["next_charge_date"],
    )


async def has_synced() -> bool:
    """False until the first sync stored anyone; until then "not found" means nothing."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM patreon_members)"))


def is_entitled(member: PatreonMember | None, eligible_tier_ids) -> bool:
    eligible = {str(tier) for tier in eligible_tier_ids}
    return bool(member and member.patron_status == "active_patron" and eligible.intersection(member.tier_ids))
