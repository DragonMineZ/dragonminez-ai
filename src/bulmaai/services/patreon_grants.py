from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bulmaai.database.db import get_pool


class PatreonGrantKind(StrEnum):
    SELF = "self"
    GIFT = "gift"


@dataclass(frozen=True, slots=True)
class PatreonLink:
    discord_user_id: int
    discord_username: str
    patreon_user_id: str
    patreon_member_id: str | None
    patreon_full_name: str | None
    patron_status: str | None
    tier_ids: tuple[str, ...]
    last_charge_date: Any
    entitlement_active: bool


@dataclass(frozen=True, slots=True)
class PatreonGrant:
    owner_discord_user_id: int
    beneficiary_discord_user_id: int
    beneficiary_discord_username: str
    minecraft_username: str
    kind: PatreonGrantKind
    active: bool
    source_pr_url: str | None = None


def _row_to_grant(row: Any) -> PatreonGrant:
    return PatreonGrant(
        owner_discord_user_id=int(row["owner_discord_user_id"]),
        beneficiary_discord_user_id=int(row["beneficiary_discord_user_id"]),
        beneficiary_discord_username=str(row["beneficiary_discord_username"]),
        minecraft_username=str(row["minecraft_username"]),
        kind=PatreonGrantKind(str(row["kind"])),
        active=bool(row["active"]),
        source_pr_url=row["source_pr_url"],
    )


async def upsert_patreon_link(link: PatreonLink) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO patreon_links (
                discord_user_id,
                discord_username,
                patreon_user_id,
                patreon_member_id,
                patreon_full_name,
                patron_status,
                tier_ids,
                last_charge_date,
                entitlement_active,
                linked_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now(), now())
            ON CONFLICT (discord_user_id)
            DO UPDATE SET
                discord_username = EXCLUDED.discord_username,
                patreon_user_id = EXCLUDED.patreon_user_id,
                patreon_member_id = EXCLUDED.patreon_member_id,
                patreon_full_name = EXCLUDED.patreon_full_name,
                patron_status = EXCLUDED.patron_status,
                tier_ids = EXCLUDED.tier_ids,
                last_charge_date = EXCLUDED.last_charge_date,
                entitlement_active = EXCLUDED.entitlement_active,
                updated_at = now()
            """,
            link.discord_user_id,
            link.discord_username,
            link.patreon_user_id,
            link.patreon_member_id,
            link.patreon_full_name,
            link.patron_status,
            list(link.tier_ids),
            link.last_charge_date,
            link.entitlement_active,
        )


async def get_patreon_link(discord_user_id: int) -> PatreonLink | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                discord_user_id,
                discord_username,
                patreon_user_id,
                patreon_member_id,
                patreon_full_name,
                patron_status,
                tier_ids,
                last_charge_date,
                entitlement_active
            FROM patreon_links
            WHERE discord_user_id = $1
            """,
            int(discord_user_id),
        )
    if row is None:
        return None
    return PatreonLink(
        discord_user_id=int(row["discord_user_id"]),
        discord_username=str(row["discord_username"]),
        patreon_user_id=str(row["patreon_user_id"]),
        patreon_member_id=row["patreon_member_id"],
        patreon_full_name=row["patreon_full_name"],
        patron_status=row["patron_status"],
        tier_ids=tuple(str(tier_id) for tier_id in row["tier_ids"]),
        last_charge_date=row["last_charge_date"],
        entitlement_active=bool(row["entitlement_active"]),
    )


async def get_patreon_link_by_member_id(member_id: str) -> PatreonLink | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                discord_user_id,
                discord_username,
                patreon_user_id,
                patreon_member_id,
                patreon_full_name,
                patron_status,
                tier_ids,
                last_charge_date,
                entitlement_active
            FROM patreon_links
            WHERE patreon_member_id = $1
            """,
            member_id,
        )
    if row is None:
        return None
    return PatreonLink(
        discord_user_id=int(row["discord_user_id"]),
        discord_username=str(row["discord_username"]),
        patreon_user_id=str(row["patreon_user_id"]),
        patreon_member_id=row["patreon_member_id"],
        patreon_full_name=row["patreon_full_name"],
        patron_status=row["patron_status"],
        tier_ids=tuple(str(tier_id) for tier_id in row["tier_ids"]),
        last_charge_date=row["last_charge_date"],
        entitlement_active=bool(row["entitlement_active"]),
    )


async def update_link_entitlement(
    *,
    discord_user_id: int,
    patron_status: str | None,
    tier_ids: tuple[str, ...],
    last_charge_date: Any,
    entitlement_active: bool,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE patreon_links
            SET
                patron_status = $2,
                tier_ids = $3,
                last_charge_date = $4,
                entitlement_active = $5,
                updated_at = now()
            WHERE discord_user_id = $1
            """,
            int(discord_user_id),
            patron_status,
            list(tier_ids),
            last_charge_date,
            entitlement_active,
        )


async def upsert_whitelist_grant(grant: PatreonGrant) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO patreon_whitelist_grants (
                owner_discord_user_id,
                beneficiary_discord_user_id,
                beneficiary_discord_username,
                minecraft_username,
                kind,
                active,
                source_pr_url,
                created_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
            ON CONFLICT (owner_discord_user_id, beneficiary_discord_user_id, kind)
            DO UPDATE SET
                beneficiary_discord_username = EXCLUDED.beneficiary_discord_username,
                minecraft_username = EXCLUDED.minecraft_username,
                active = EXCLUDED.active,
                source_pr_url = EXCLUDED.source_pr_url,
                updated_at = now()
            """,
            int(grant.owner_discord_user_id),
            int(grant.beneficiary_discord_user_id),
            grant.beneficiary_discord_username,
            grant.minecraft_username,
            grant.kind.value,
            grant.active,
            grant.source_pr_url,
        )


async def count_active_gifts_for_owner(owner_discord_user_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT count(*)
            FROM patreon_whitelist_grants
            WHERE owner_discord_user_id = $1
              AND kind = 'gift'
              AND active = TRUE
            """,
            int(owner_discord_user_id),
        )
    return int(value or 0)


async def list_active_grants_for_owner(owner_discord_user_id: int) -> list[PatreonGrant]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                owner_discord_user_id,
                beneficiary_discord_user_id,
                beneficiary_discord_username,
                minecraft_username,
                kind,
                active,
                source_pr_url
            FROM patreon_whitelist_grants
            WHERE owner_discord_user_id = $1
              AND active = TRUE
            ORDER BY created_at ASC
            """,
            int(owner_discord_user_id),
        )
    return [_row_to_grant(row) for row in rows]


async def deactivate_grants_for_owner(owner_discord_user_id: int) -> list[PatreonGrant]:
    grants = await list_active_grants_for_owner(owner_discord_user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE patreon_whitelist_grants
            SET active = FALSE,
                updated_at = now()
            WHERE owner_discord_user_id = $1
              AND active = TRUE
            """,
            int(owner_discord_user_id),
        )
    return grants
