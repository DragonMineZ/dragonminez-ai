"""Known scam images (table scam_image_hashes), matched by 64-bit dHash: survives resizing and
re-encoding, not crops. The hash list is cached in memory; call load() at startup."""

import io
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from PIL import Image, UnidentifiedImageError

from bulmaai.database.db import get_pool

log = logging.getLogger(__name__)

MIN_SIDE = 100  # ponytail: icon/emoji-sized images hash to noise; skip them
THUMB_SIDE = 256  # Discord's media proxy resizes first, so a check downloads a few KB, not the full image
PREVIEW_SIDE = 1024  # the copy re-uploaded into review alerts; the original may be purged before staff look
MAX_FETCH_BYTES = 8 * 1024 * 1024

_hashes: dict[int, int] = {}  # row id -> unsigned hash
_session: aiohttp.ClientSession | None = None


@dataclass(frozen=True, slots=True)
class ScamHash:
    id: int
    hash: int
    source: str
    added_by: int | None
    note: str | None
    hits: int
    last_hit_at: datetime | None
    created_at: datetime


# --- hashing ------------------------------------------------------------------------------------


def dhash(data: bytes) -> int | None:
    """Difference hash: 9x8 grayscale, one bit per left>right neighbour pair. None if undecodable."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            pixels = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    except (OSError, UnidentifiedImageError, ValueError):
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return bits


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_db(value: int) -> int:
    """Unsigned 64-bit -> Postgres BIGINT."""
    return value - (1 << 64) if value >= 1 << 63 else value


def from_db(value: int) -> int:
    return value + (1 << 64) if value < 0 else value


def is_hashable(attachment) -> bool:
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    width, height = getattr(attachment, "width", None), getattr(attachment, "height", None)
    return content_type.startswith("image/") and bool(width and height) and min(width, height) >= MIN_SIDE


def _thumbnail_url(proxy_url: str, side: int = THUMB_SIDE) -> str:
    parts = urlsplit(proxy_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key not in ("width", "height")]
    query += [("width", str(side)), ("height", str(side))]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _get(url: str) -> bytes | None:
    global _session
    if _session is None or _session.closed:
        # ponytail: one process-wide session, never closed; the bot runs until the process exits.
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    try:
        async with _session.get(url) as response:
            if response.status != 200 or (response.content_length or 0) > MAX_FETCH_BYTES:
                return None
            return await response.read()
    except (aiohttp.ClientError, TimeoutError):
        return None


async def hash_attachment(attachment) -> int | None:
    """dHash of an image attachment via the resized proxy copy (the original as a fallback)."""
    if not is_hashable(attachment):
        return None
    data = await _get(_thumbnail_url(attachment.proxy_url)) if getattr(attachment, "proxy_url", None) else None
    if data is None and (getattr(attachment, "size", 0) or 0) <= MAX_FETCH_BYTES:
        data = await _get(attachment.url)
    return dhash(data) if data else None


async def fetch_preview(attachment) -> bytes | None:
    """A reviewable copy of the image (resized proxy, original as a fallback) to re-upload into an alert."""
    data = await _get(_thumbnail_url(attachment.proxy_url, PREVIEW_SIDE)) if getattr(attachment, "proxy_url", None) else None
    if data is None and (getattr(attachment, "size", 0) or 0) <= MAX_FETCH_BYTES:
        data = await _get(attachment.url)
    return data


# --- the list -----------------------------------------------------------------------------------


def _row(row) -> ScamHash:
    return ScamHash(
        id=row["id"],
        hash=from_db(row["hash"]),
        source=row["source"],
        added_by=row["added_by"],
        note=row["note"],
        hits=row["hits"],
        last_hit_at=row["last_hit_at"],
        created_at=row["created_at"],
    )


async def load() -> int:
    pool = await get_pool()
    rows = await pool.fetch("SELECT id, hash FROM scam_image_hashes")
    _hashes.clear()
    _hashes.update({row["id"]: from_db(row["hash"]) for row in rows})
    return len(_hashes)


def is_empty() -> bool:
    return not _hashes


def match(value: int, max_distance: int) -> int | None:
    """Row id of the closest known hash within max_distance bits, if any."""
    # ponytail: linear scan; fine into the thousands of hashes, a BK-tree if the list ever gets huge.
    best = min(_hashes.items(), key=lambda item: distance(item[1], value), default=None)
    return best[0] if best is not None and distance(best[1], value) <= max_distance else None


async def add(value: int, *, source: str, added_by: int | None = None, note: str | None = None) -> int:
    """Returns the row id (the existing one when this exact hash is already listed)."""
    pool = await get_pool()
    hash_id = await pool.fetchval(
        """
        INSERT INTO scam_image_hashes (hash, source, added_by, note) VALUES ($1, $2, $3, $4)
        ON CONFLICT (hash) DO UPDATE SET hash = EXCLUDED.hash
        RETURNING id
        """,
        to_db(value),
        source,
        added_by,
        note,
    )
    _hashes[hash_id] = value
    return hash_id


async def remove(hash_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute("DELETE FROM scam_image_hashes WHERE id = $1", hash_id)
    _hashes.pop(hash_id, None)
    return result.endswith(" 1")


async def note_hit(hash_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE scam_image_hashes SET hits = hits + 1, last_hit_at = now() WHERE id = $1", hash_id
    )


async def list_hashes(limit: int = 50) -> list[ScamHash]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM scam_image_hashes ORDER BY id DESC LIMIT $1", limit)
    return [_row(row) for row in rows]

