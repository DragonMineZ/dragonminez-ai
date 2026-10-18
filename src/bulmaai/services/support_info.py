"""Staff-curated support info (cogs/support_info.py): temporary known issues with their workaround, posted publicly
every 24h, and permanent custom answers. Every active entry goes into the support AI's context (ai_client)."""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bulmaai.database.db import get_pool

KIND_TEMPORARY = "temporary"
KIND_PERMANENT = "permanent"
KINDS = (KIND_TEMPORARY, KIND_PERMANENT)
STATUSES = ("suggested", "active", "discarded", "removed")
TITLE_MAX = 100
BODY_MAX = 1500
POST_INTERVAL = timedelta(hours=24)
CACHE_SECONDS = 60.0

_COLUMNS = (
    "id, kind, title, body, status, source, ticket_id, expires_at, created_by, updated_by, "
    "review_channel_id, review_message_id, created_at, updated_at"
)
_ACTIVE = "status = 'active' AND (expires_at IS NULL OR expires_at > now())"


@dataclass(frozen=True, slots=True)
class SupportInfo:
    id: int
    kind: str
    title: str
    body: str
    status: str
    source: str
    ticket_id: int | None
    expires_at: datetime | None
    created_by: int | None
    updated_by: int | None
    review_channel_id: int | None
    review_message_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DailyPost:
    """The previous daily post, handed to whoever claimed the next one so it can delete it."""

    channel_id: int | None
    message_id: int | None


def _info(row: Any) -> SupportInfo | None:
    return SupportInfo(**{key: row[key] for key in SupportInfo.__slots__}) if row else None


def validate(kind: str, title: str, body: str) -> tuple[str, str]:
    """Stripped (title, body); ValueError with a message staff can read when something is off."""
    if kind not in KINDS:
        raise ValueError("Kind must be temporary or permanent.")
    title, body = (title or "").strip(), (body or "").strip()
    if not title or not body:
        raise ValueError("Title and text can't be empty.")
    if len(title) > TITLE_MAX:
        raise ValueError(f"Title is too long ({len(title)}/{TITLE_MAX} characters).")
    if len(body) > BODY_MAX:
        raise ValueError(f"Text is too long ({len(body)}/{BODY_MAX} characters).")
    return title, body


# ---- active-entry cache for the AI (one DB read per CACHE_SECONDS, dropped on every write) ----------------------

_cache: tuple[float, list[SupportInfo]] | None = None


def invalidate_cache() -> None:
    global _cache
    _cache = None


async def cached_active(*, pool: Any | None = None) -> list[SupportInfo]:
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    entries = await list_active(pool=pool)
    _cache = (time.monotonic(), entries)
    return entries


# ---- entries --------------------------------------------------------------------------------------------------------


async def add_info(
    *,
    kind: str,
    title: str,
    body: str,
    created_by: int | None,
    expires_at: datetime | None = None,
    status: str = "active",
    source: str = "manual",
    ticket_id: int | None = None,
    pool: Any | None = None,
) -> SupportInfo:
    title, body = validate(kind, title, body)
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"""
        INSERT INTO support_info (kind, title, body, status, source, ticket_id, expires_at, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
        RETURNING {_COLUMNS}
        """,
        kind,
        title,
        body,
        status,
        source,
        ticket_id,
        expires_at if kind == KIND_TEMPORARY else None,
        created_by,
    )
    invalidate_cache()
    return _info(row)


async def update_info(
    info_id: int,
    *,
    kind: str,
    title: str,
    body: str,
    expires_at: datetime | None,
    updated_by: int | None,
    only_from: Sequence[str] = ("active", "suggested"),
    pool: Any | None = None,
) -> SupportInfo | None:
    """None when the entry doesn't exist or isn't in one of only_from (by default: was discarded/removed)."""
    title, body = validate(kind, title, body)
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"""
        UPDATE support_info SET kind = $2, title = $3, body = $4, expires_at = $5, updated_by = $6, updated_at = now()
        WHERE id = $1 AND status = ANY($7::text[])
        RETURNING {_COLUMNS}
        """,
        info_id,
        kind,
        title,
        body,
        expires_at if kind == KIND_TEMPORARY else None,
        updated_by,
        list(only_from),
    )
    invalidate_cache()
    return _info(row)


async def set_status(
    info_id: int,
    status: str,
    *,
    updated_by: int | None,
    only_from: Sequence[str] = ("suggested",),
    default_expires_at: datetime | None = None,
    pool: Any | None = None,
) -> SupportInfo | None:
    """Moves the entry to status if it is in one of only_from. Two racing clicks: the first wins, the other gets None.
    default_expires_at: set on a temporary entry that has no expiry yet, in the same update."""
    if status not in STATUSES:
        raise ValueError(f"Unknown status {status!r}.")
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        f"""
        UPDATE support_info SET status = $2, updated_by = $3, updated_at = now(),
            expires_at = CASE WHEN kind = 'temporary' AND expires_at IS NULL THEN $5 ELSE expires_at END
        WHERE id = $1 AND status = ANY($4::text[])
        RETURNING {_COLUMNS}
        """,
        info_id,
        status,
        updated_by,
        list(only_from),
        default_expires_at,
    )
    invalidate_cache()
    return _info(row)


async def set_review_message(info_id: int, channel_id: int, message_id: int, *, pool: Any | None = None) -> None:
    """Where a suggestion's staff review card was posted."""
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute(
        "UPDATE support_info SET review_channel_id = $2, review_message_id = $3 WHERE id = $1",
        info_id,
        channel_id,
        message_id,
    )


async def get_info(info_id: int, *, pool: Any | None = None) -> SupportInfo | None:
    resolved_pool = pool or await get_pool()
    return _info(await resolved_pool.fetchrow(f"SELECT {_COLUMNS} FROM support_info WHERE id = $1", info_id))


async def list_active(*, pool: Any | None = None) -> list[SupportInfo]:
    """Active, unexpired entries: temporary first, newest first."""
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        f"SELECT {_COLUMNS} FROM support_info WHERE {_ACTIVE} ORDER BY (kind = 'temporary') DESC, created_at DESC"
    )
    return [_info(row) for row in rows]


async def list_by_status(status: str, *, pool: Any | None = None) -> list[SupportInfo]:
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        f"SELECT {_COLUMNS} FROM support_info WHERE status = $1 ORDER BY created_at DESC", status
    )
    return [_info(row) for row in rows]


def render_for_ai(entries: Sequence[SupportInfo], *, limit_chars: int = 6000) -> str:
    """'### [kind] title' + body per entry, in the given order; stops before the entry that would pass limit_chars."""
    blocks: list[str] = []
    used = 0
    for entry in entries:
        block = f"### [{entry.kind}] {entry.title}\n{entry.body}"
        cost = len(block) + (2 if blocks else 0)
        if used + cost > limit_chars:
            break
        blocks.append(block)
        used += cost
    return "\n\n".join(blocks)


# ---- daily public post (single row, id = 1) ------------------------------------------------------------------------


async def claim_daily_post(*, interval: timedelta = POST_INTERVAL, pool: Any | None = None) -> DailyPost | None:
    """The previous post if a new one is due (never posted, or interval has passed), else None. Claiming stamps
    posted_at at once, so a second loop running at the same time gets None."""
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute("INSERT INTO support_info_post (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    row = await resolved_pool.fetchrow(
        """
        UPDATE support_info_post SET posted_at = now()
        WHERE id = 1 AND (posted_at IS NULL OR posted_at <= now() - $1::interval)
        RETURNING channel_id, message_id
        """,
        interval,
    )
    return DailyPost(row["channel_id"], row["message_id"]) if row else None


async def record_daily_post(
    channel_id: int | None, message_id: int | None, *, retry: bool = False, pool: Any | None = None
) -> None:
    """The post that just went out (message_id None: nothing was active, so nothing was posted). retry: sending
    failed, so the next loop tries again instead of waiting a day."""
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute(
        "UPDATE support_info_post SET channel_id = $1, message_id = $2, "
        "posted_at = CASE WHEN $3::boolean THEN NULL ELSE now() END WHERE id = 1",
        channel_id,
        message_id,
        retry,
    )
