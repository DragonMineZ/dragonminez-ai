"""Ticket records for the in-house ticket system (cogs/tickets.py), kept in PostgreSQL via the shared asyncpg pool."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bulmaai.database.db import get_pool

STATUS_CREATING = "creating"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_DELETED = "deleted"

# A crash between reserving a number and creating the channel leaves a 'creating' row; stop counting it after this.
_CREATING_GRACE = "5 minutes"
_COLUMNS = (
    "ticket_id, guild_id, owner_id, channel_id, category, status, channel_name, language, "
    "control_message_id, claimed_by, closed_by, close_reason, created_at, closed_at"
)
_MAX_SLUG_LENGTH = 40


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket_id: int
    guild_id: int
    owner_id: int
    channel_id: int | None
    category: str
    status: str
    channel_name: str | None
    language: str
    control_message_id: int | None
    claimed_by: int | None
    closed_by: int | None
    close_reason: str | None
    created_at: datetime
    closed_at: datetime | None


def _ticket(row: Any) -> Ticket | None:
    return Ticket(**{key: row[key] for key in Ticket.__slots__}) if row else None


def sanitize_slug(raw: str, fallback: str) -> str:
    """Model- or user-derived text → a safe Discord channel slug (a-z, 0-9, dashes)."""
    ascii_text = unicodedata.normalize("NFKD", str(raw or "")).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:_MAX_SLUG_LENGTH].strip("-")
    return slug or fallback


def open_channel_name(slug: str, ticket_id: int) -> str:
    return f"{slug}-{ticket_id:04d}"


def closed_channel_name(ticket_id: int) -> str:
    return f"closed-{ticket_id:04d}"


async def reserve_ticket(
    *,
    guild_id: int,
    owner_id: int,
    category: str,
    language: str,
    max_open: int,
    pool: Any | None = None,
) -> Ticket | None:
    """Atomically take the next number. The counter row lock serializes the open-ticket limit check too.
    Returns None when the owner is at their limit."""
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        async with conn.transaction():
            last = await conn.fetchval("SELECT last_number FROM ticket_counter WHERE id = 1 FOR UPDATE")
            active = await conn.fetchval(
                f"""
                SELECT count(*) FROM tickets
                WHERE owner_id = $1
                  AND (status = 'open'
                       OR (status = 'creating' AND created_at > now() - interval '{_CREATING_GRACE}'))
                """,
                owner_id,
            )
            if active >= max_open:
                return None
            ticket_id = last + 1
            await conn.execute("UPDATE ticket_counter SET last_number = $1 WHERE id = 1", ticket_id)
            row = await conn.fetchrow(
                f"""
                INSERT INTO tickets (ticket_id, guild_id, owner_id, category, language)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING {_COLUMNS}
                """,
                ticket_id,
                guild_id,
                owner_id,
                category,
                language,
            )
    return _ticket(row)


async def attach_channel(
    ticket_id: int,
    *,
    channel_id: int,
    channel_name: str,
    language: str,
    control_message_id: int | None,
    pool: Any | None = None,
) -> None:
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute(
        """
        UPDATE tickets
        SET channel_id = $2, channel_name = $3, language = $4, control_message_id = $5, status = 'open'
        WHERE ticket_id = $1
        """,
        ticket_id,
        channel_id,
        channel_name,
        language,
        control_message_id,
    )


async def abandon_ticket(ticket_id: int, *, pool: Any | None = None) -> None:
    """Channel creation failed: free the owner's slot (the number stays burned)."""
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute(
        "DELETE FROM tickets WHERE ticket_id = $1 AND status = 'creating'", ticket_id
    )


async def get_ticket_by_channel(channel_id: int, *, pool: Any | None = None) -> Ticket | None:
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"SELECT {_COLUMNS} FROM tickets WHERE channel_id = $1 AND status IN ('open', 'closed')", channel_id
    )
    return _ticket(row)


async def get_open_tickets_by_owner(owner_id: int, *, pool: Any | None = None) -> list[Ticket]:
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        f"SELECT {_COLUMNS} FROM tickets WHERE owner_id = $1 AND status = 'open' AND channel_id IS NOT NULL",
        owner_id,
    )
    return [_ticket(row) for row in rows]


async def list_active_tickets(*, pool: Any | None = None) -> list[Ticket]:
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        f"SELECT {_COLUMNS} FROM tickets WHERE status IN ('open', 'closed') AND channel_id IS NOT NULL"
    )
    return [_ticket(row) for row in rows]


async def mark_closed(
    channel_id: int,
    *,
    closed_by: int | None,
    reason: str | None,
    pool: Any | None = None,
) -> Ticket | None:
    """open → closed. None when it wasn't open, so two racing closes only run once."""
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"""
        UPDATE tickets SET status = 'closed', closed_by = $2, close_reason = $3, closed_at = now()
        WHERE channel_id = $1 AND status = 'open'
        RETURNING {_COLUMNS}
        """,
        channel_id,
        closed_by,
        reason,
    )
    return _ticket(row)


async def mark_reopened(channel_id: int, *, pool: Any | None = None) -> Ticket | None:
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"""
        UPDATE tickets SET status = 'open', closed_by = NULL, close_reason = NULL, closed_at = NULL
        WHERE channel_id = $1 AND status = 'closed'
        RETURNING {_COLUMNS}
        """,
        channel_id,
    )
    return _ticket(row)


async def mark_deleted(channel_id: int, *, pool: Any | None = None) -> None:
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute(
        "UPDATE tickets SET status = 'deleted', deleted_at = now() "
        "WHERE channel_id = $1 AND status IN ('open', 'closed')",
        channel_id,
    )


async def ticket_info_for_channels(
    channel_ids: list[int], *, pool: Any | None = None
) -> dict[int, dict[str, Any]]:
    """Admin panel: ticket number, category, status and claimer per channel."""
    if not channel_ids:
        return {}
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        "SELECT ticket_id, channel_id, category, status, claimed_by, owner_id "
        "FROM tickets WHERE channel_id = ANY($1::bigint[])",
        channel_ids,
    )
    return {int(row["channel_id"]): dict(row) for row in rows}
