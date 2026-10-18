"""Ticket records for the in-house ticket system (cogs/tickets.py), kept in PostgreSQL via the shared asyncpg pool."""

import re
import unicodedata
from collections.abc import Iterable, Sequence
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
    "control_message_id, claimed_by, closed_by, close_reason, created_at, closed_at, closed_periods, delete_at"
)
_MAX_SLUG_LENGTH = 40

# A closed stretch: (closed at, re-opened at), or (closed at, None) while it still is.
Span = tuple[datetime, datetime | None]


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
    # Finished stretches only; the current one is closed_at while the status is closed (see closed_spans).
    closed_periods: tuple[tuple[datetime, datetime], ...] = ()
    # Closed as solved, or a deletion under way: the sweep deletes the channel then, unless staff re-open it first.
    delete_at: datetime | None = None


def _ticket(row: Any) -> Ticket | None:
    if not row:
        return None
    values = {key: row[key] for key in Ticket.__slots__}
    # tstzrange[] → (start, end); a range missing a bound would hide everything, so it's dropped.
    values["closed_periods"] = tuple(
        (period.lower, period.upper) for period in values["closed_periods"] if period.lower and period.upper
    )
    return Ticket(**values)


def closed_spans(ticket: Ticket | None) -> list[Span]:
    """Every stretch the ticket spent closed, oldest first. No tickets row: none."""
    if ticket is None:
        return []
    spans: list[Span] = list(ticket.closed_periods)
    if ticket.status == STATUS_CLOSED and ticket.closed_at is not None:
        spans.append((ticket.closed_at, None))
    return spans


def closed_span_index(when: datetime, spans: Sequence[Span]) -> int | None:
    """Which closed stretch `when` falls in, if any."""
    return next((i for i, (start, end) in enumerate(spans) if start <= when and (end is None or when < end)), None)


@dataclass(slots=True)
class ClosedGap:
    """Stands in for one closed stretch's hidden messages: the first one's time and how many there were."""

    created_at: datetime
    count: int = 1

    @property
    def notice(self) -> str:
        sent = "1 message was" if self.count == 1 else f"{self.count} messages were"
        return f"{sent} sent while this ticket was closed."


def hide_closed_messages(messages: Iterable[Any], spans: Sequence[Span], *, bot_id: int | None) -> list[Any]:
    """Oldest-first messages minus those sent while the ticket was closed (our bot's own cards stay). Each closed
    stretch leaves one ClosedGap where its first hidden message was."""
    entries: list[Any] = []
    gaps: dict[int, ClosedGap] = {}
    for message in messages:
        span = closed_span_index(message.created_at, spans)
        if span is None or message.author.id == bot_id:
            entries.append(message)
        elif span in gaps:
            gaps[span].count += 1
        else:
            gaps[span] = ClosedGap(message.created_at)
            entries.append(gaps[span])
    return entries


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
    pool: Any | None = None,
) -> Ticket | None:
    """Atomically take the next number. The counter row lock serializes the open-ticket limit check too.
    Returns None when the owner already has an open (or still creating) ticket."""
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
            if active:
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


async def get_ticket_by_id(ticket_id: int, *, pool: Any | None = None) -> Ticket | None:
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(f"SELECT {_COLUMNS} FROM tickets WHERE ticket_id = $1", ticket_id)
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


async def list_due_solved(*, pool: Any | None = None) -> list[Ticket]:
    """Closed tickets past their delete_at: solved ones whose re-open window is over, and deletions a restart cut
    short."""
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        f"SELECT {_COLUMNS} FROM tickets WHERE status = 'closed' AND delete_at <= now() AND channel_id IS NOT NULL"
    )
    return [_ticket(row) for row in rows]


async def mark_closed(
    channel_id: int,
    *,
    closed_by: int | None,
    reason: str | None,
    delete_at: datetime | None = None,
    pool: Any | None = None,
) -> Ticket | None:
    """open → closed. None when it wasn't open, so two racing closes only run once. delete_at: closed as solved."""
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"""
        UPDATE tickets SET status = 'closed', closed_by = $2, close_reason = $3, closed_at = now(), delete_at = $4
        WHERE channel_id = $1 AND status = 'open'
        RETURNING {_COLUMNS}
        """,
        channel_id,
        closed_by,
        reason,
        delete_at,
    )
    return _ticket(row)


async def schedule_delete(channel_id: int, delete_at: datetime, *, pool: Any | None = None) -> Ticket | None:
    """A closed ticket is about to be deleted: the deadline is stored first, so the sweep finishes it after a restart.
    None when it isn't closed."""
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"UPDATE tickets SET delete_at = $2 WHERE channel_id = $1 AND status = 'closed' RETURNING {_COLUMNS}",
        channel_id,
        delete_at,
    )
    return _ticket(row)


async def mark_reopened(channel_id: int, *, pool: Any | None = None) -> Ticket | None:
    """closed → open; the finished closed stretch goes into closed_periods (SET reads the old closed_at). A solved
    ticket past its delete_at stays closed for the sweep."""
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"""
        UPDATE tickets SET status = 'open', closed_by = NULL, close_reason = NULL, closed_at = NULL, delete_at = NULL,
            awaiting_owner_since = NULL, owner_reminded_at = NULL,
            closed_periods = closed_periods || tstzrange(closed_at, now())
        WHERE channel_id = $1 AND status = 'closed' AND (delete_at IS NULL OR delete_at > now())
        RETURNING {_COLUMNS}
        """,
        channel_id,
    )
    return _ticket(row)


async def set_language(channel_id: int, language: str, *, pool: Any | None = None) -> Ticket | None:
    """An open ticket's support language; None when it isn't open."""
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"UPDATE tickets SET language = $2 WHERE channel_id = $1 AND status = 'open' RETURNING {_COLUMNS}",
        channel_id,
        language,
    )
    return _ticket(row)


async def note_ticket_message(channel_id: int, author_id: int, *, pool: Any | None = None) -> None:
    """A message in a ticket channel (an AI answer passes the bot's id). Someone other than the owner starts the
    owner's reply countdown if it isn't running; the owner clears it. Writes only when that state flips."""
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute(
        """
        UPDATE tickets
        SET awaiting_owner_since = CASE WHEN owner_id = $2 THEN NULL ELSE now() END, owner_reminded_at = NULL
        WHERE channel_id = $1 AND status = 'open'
          AND ((owner_id = $2 AND awaiting_owner_since IS NOT NULL) OR (owner_id <> $2 AND awaiting_owner_since IS NULL))
        """,
        channel_id,
        author_id,
    )


async def claim_owner_reminders(hours: int, *, pool: Any | None = None) -> list[Ticket]:
    """Open tickets whose owner hasn't replied for `hours`, each claimed once per silence (owner_reminded_at)."""
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        f"""
        UPDATE tickets SET owner_reminded_at = now()
        WHERE status = 'open' AND channel_id IS NOT NULL AND owner_reminded_at IS NULL
          AND awaiting_owner_since <= now() - make_interval(hours => $1)
        RETURNING {_COLUMNS}
        """,
        hours,
    )
    return [_ticket(row) for row in rows]


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
