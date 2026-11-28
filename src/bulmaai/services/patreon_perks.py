"""Patreon tier perks members claim from the ticket panel (custom clothing, hairstyle, NPC skin).

Each perk is claimed once per member. A claim is theirs for good once made: losing the role or the tier later
never takes it back (Bruno, 2026-11-27). Only a staff-rejected claim frees the perk for a new claim."""

from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool
from bulmaai.services.patreon_members import PatreonMember

PERKS = ("clothing", "hairstyle", "npc_skin")


@dataclass(frozen=True, slots=True)
class PerkClaim:
    claim_id: int
    discord_user_id: int
    perk: str
    status: str
    ticket_channel_id: int | None
    created_at: datetime
    minecraft_username: str = ""
    details: str = ""
    decided_by: int | None = None
    notion_page_id: str | None = None
    pr_url: str | None = None


def can_claim(member: PatreonMember | None, perk_tier_titles) -> bool:
    """An active patron on a perk tier whose last payment went through."""
    titles = {title.casefold() for title in perk_tier_titles}
    return bool(
        member
        and member.patron_status == "active_patron"
        and (member.last_charge_status or "").casefold() == "paid"
        and titles.intersection(title.casefold() for title in member.tier_titles)
    )


def _claim(row) -> PerkClaim:
    return PerkClaim(
        claim_id=row["id"],
        discord_user_id=row["discord_user_id"],
        perk=row["perk"],
        status=row["status"],
        ticket_channel_id=row["ticket_channel_id"],
        created_at=row["created_at"],
        minecraft_username=row["minecraft_username"],
        details=row["details"],
        decided_by=row["decided_by"],
        notion_page_id=row["notion_page_id"],
        pr_url=row["pr_url"],
    )


async def list_claims(discord_user_id: int) -> list[PerkClaim]:
    """Their claims that still hold the perk (anything but rejected), oldest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM patreon_perk_claims WHERE discord_user_id = $1 AND status <> 'rejected' ORDER BY created_at",
            discord_user_id,
        )
    return [_claim(row) for row in rows]


async def claim(*, discord_user_id: int, perk: str, minecraft_username: str, details: str) -> int | None:
    """The new claim's id, or None when they already hold this perk (the unique index decides, so two submits
    racing each other can't both win)."""
    if perk not in PERKS:
        raise ValueError(f"unknown perk {perk!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO patreon_perk_claims (discord_user_id, perk, minecraft_username, details)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (discord_user_id, perk) WHERE status <> 'rejected' DO NOTHING
            RETURNING id
            """,
            discord_user_id, perk, minecraft_username, details,
        )


async def attach_ticket(claim_id: int, channel_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE patreon_perk_claims SET ticket_channel_id = $2 WHERE id = $1", claim_id, channel_id)


async def release(claim_id: int) -> None:
    """Undo a claim whose ticket couldn't be opened, so they can try again."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM patreon_perk_claims WHERE id = $1 AND ticket_channel_id IS NULL", claim_id)


async def get_claim(claim_id: int) -> PerkClaim | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM patreon_perk_claims WHERE id = $1", claim_id)
    return _claim(row) if row else None


async def decide(claim_id: int, status: str, decided_by: int) -> PerkClaim | None:
    """Accept or reject a waiting claim; the first click wins (None when it was already decided)."""
    if status not in ("accepted", "rejected"):
        raise ValueError(f"unknown decision {status!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE patreon_perk_claims SET status = $2, decided_by = $3 WHERE id = $1 AND status = 'pending' RETURNING *",
            claim_id, status, decided_by,
        )
    return _claim(row) if row else None


async def set_notion_page(claim_id: int, page_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE patreon_perk_claims SET notion_page_id = $2 WHERE id = $1", claim_id, page_id)


async def set_pr_url(claim_id: int, url: str) -> bool:
    """Stores the mod PR on an accepted claim that has none yet; False when another ship got there first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        done = await conn.execute(
            "UPDATE patreon_perk_claims SET pr_url = $2 WHERE id = $1 AND status = 'accepted' AND pr_url IS NULL",
            claim_id, url,
        )
    return done.endswith(" 1")
