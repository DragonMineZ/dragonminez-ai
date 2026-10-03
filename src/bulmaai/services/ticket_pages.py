"""Hosted HTML ticket transcripts: one file per ticket on disk, an unguessable token in the URL, expiry in PostgreSQL.

The file lives at <ticket_transcript_dir>/<token>.html and is served by web/routes_transcripts.py. The token is the
only credential, so it is 192 random bits and never listed anywhere except staff channels, the panel and the owner's DM.
"""

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bulmaai.database.db import get_pool

log = logging.getLogger(__name__)

TOKEN_PATTERN = r"[A-Za-z0-9_-]{32}"
_TOKEN_RE = re.compile(TOKEN_PATTERN)
# A file whose row isn't there (yet) is only an orphan once the close pipeline has had time to record it.
_ORPHAN_GRACE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class StoredPage:
    token: str
    expires_at: datetime | None  # None = kept forever


def new_token() -> str:
    return secrets.token_urlsafe(24)


def page_url(settings: Any, token: str) -> str:
    return f"{settings.ticket_transcript_public_url.rstrip('/')}/t/{token}"


def page_path(settings: Any, token: str) -> Path:
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("Invalid transcript token.")
    return Path(settings.ticket_transcript_dir) / f"{token}.html"


def expiry_from_now(settings: Any) -> datetime | None:
    days = settings.ticket_transcript_retention_days
    return datetime.now(timezone.utc) + timedelta(days=days) if days > 0 else None


async def save_page(settings: Any, html: bytes) -> StoredPage:
    token = new_token()
    path = page_path(settings, token)

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(html)

    await asyncio.to_thread(write)
    return StoredPage(token=token, expires_at=expiry_from_now(settings))


def remove_file(settings: Any, token: str) -> None:
    try:
        page_path(settings, token).unlink(missing_ok=True)
    except (OSError, ValueError):
        log.exception("Could not delete transcript file", extra={"token_prefix": token[:4]})


async def get_page_for_channel(channel_id: int, *, pool: Any | None = None) -> StoredPage | None:
    row = await (pool or await get_pool()).fetchrow(
        "SELECT html_token, html_expires_at FROM ticket_transcripts "
        "WHERE channel_id = $1 AND html_token IS NOT NULL ORDER BY id DESC LIMIT 1",
        channel_id,
    )
    return StoredPage(token=row["html_token"], expires_at=row["html_expires_at"]) if row else None


async def is_servable(token: str, *, pool: Any | None = None) -> bool:
    """A token is live while its row exists and hasn't expired; the purge job removes the file later."""
    row = await (pool or await get_pool()).fetchrow(
        "SELECT html_expires_at FROM ticket_transcripts WHERE html_token = $1", token
    )
    return row is not None and (row["html_expires_at"] is None or row["html_expires_at"] > datetime.now(timezone.utc))


async def set_permanent(settings: Any, transcript_id: int, permanent: bool, *, pool: Any | None = None) -> bool:
    """Keep the page forever, or start the retention clock again from now. False if the transcript has no page."""
    expires = None if permanent else expiry_from_now(settings)
    result = await (pool or await get_pool()).execute(
        "UPDATE ticket_transcripts SET html_expires_at = $2 WHERE id = $1 AND html_token IS NOT NULL",
        transcript_id,
        expires,
    )
    return result.endswith(" 1")


async def delete_page(settings: Any, transcript_id: int, *, pool: Any | None = None) -> bool:
    """Take the HTML page down but keep the staff text record. False if there was no page."""
    resolved_pool = pool or await get_pool()
    row = await resolved_pool.fetchrow(
        "SELECT html_token FROM ticket_transcripts WHERE id = $1 AND html_token IS NOT NULL", transcript_id
    )
    if row is None:
        return False
    await resolved_pool.execute(
        "UPDATE ticket_transcripts SET html_token = NULL, html_expires_at = NULL WHERE id = $1", transcript_id
    )
    remove_file(settings, row["html_token"])
    return True


async def delete_record(settings: Any, transcript_id: int, *, pool: Any | None = None) -> bool:
    """Remove the whole transcript row (text, summary) and its HTML page."""
    row = await (pool or await get_pool()).fetchrow(
        "DELETE FROM ticket_transcripts WHERE id = $1 RETURNING html_token", transcript_id
    )
    if row is None:
        return False
    if row["html_token"]:
        remove_file(settings, row["html_token"])
    return True


async def purge(settings: Any, *, pool: Any | None = None) -> int:
    """Drop expired pages, then delete any file no row points to (superseded or failed closes). Returns files removed."""
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute(
        "UPDATE ticket_transcripts SET html_token = NULL, html_expires_at = NULL "
        "WHERE html_token IS NOT NULL AND html_expires_at < now()"
    )
    live = {row["html_token"] for row in await resolved_pool.fetch(
        "SELECT html_token FROM ticket_transcripts WHERE html_token IS NOT NULL"
    )}
    directory = Path(settings.ticket_transcript_dir)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - _ORPHAN_GRACE_SECONDS
    removed = 0
    for path in directory.glob("*.html"):
        if path.stem in live:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            log.exception("Could not purge transcript file", extra={"path": str(path)})
    return removed
