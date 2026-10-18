"""Log corpus: every crash log the log_parser cog analyzes in the main guild is kept, redacted, under CORPUS_DIR with
the chat around it, so Bruno can download a version's batch as a .zip (cogs/log_corpus.py) and study it to improve
utils/log_parser.py. Temporary by design: files go away once they are bundled into a .zip, or after RETENTION_DAYS.

Rows: log_corpus_entries (one per message + file). log_corpus_pings remembers which versions already pinged Bruno;
a bundle clears its versions' rows. Files: <message_id>-<file> (the redacted log) and <message_id>-<file>.json
(versions, root cause, the uploader's text and the messages before it). No Discord ids or names go in the files."""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import tempfile
import time
import zipfile
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bulmaai.database.db import get_pool
from bulmaai.services import log_pages
from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.utils.permissions import has_staff_rank, is_staff

log = logging.getLogger(__name__)

CORPUS_DIR = Path("data/log_corpus")
RETENTION_DAYS = 60
BEFORE_LIMIT = 10
BEFORE_WINDOW = timedelta(minutes=30)
AFTER_LIMIT = 10
AFTER_WINDOW = timedelta(hours=48)
# Messages read per history call, so skipped bot messages still leave up to 10 people's messages.
HISTORY_SCAN = 40
VERSION_MAX = 80
# Room kept free under the upload limit for the zip's own headers and the estimate being a little off.
ZIP_MARGIN = 256 * 1024

_USER_MENTION_RE = re.compile(r"<@!?\d+>")
_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
_CHANNEL_MENTION_RE = re.compile(r"<#\d+>")
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_]{3,16}(?![A-Za-z0-9_])")
# Context key for the salted hashes of the log's account names: the chat after the log is read only at download time,
# when the names themselves are long gone. Dropped before the context goes in a zip.
HIDDEN_NAMES_KEY = "_hidden_names"

README = """DragonMineZ log corpus
======================

Crash logs BulmaAI analyzed in the DragonMineZ Discord, collected to study and improve the bot's rule-based log
analyzer (src/bulmaai/utils/log_parser.py). Temporary: delete these files once that work is done, never commit them.

Layout
------
<version>/<message id>-<file name>        The log as posted, with personal details masked: <player>, <user>, <ip>,
                                           <email>, <uuid>, <server>, <redacted>.
<version>/<message id>-<file name>.json   What was around it:
    file, dmz_version, minecraft_version, forge_version, java_version, error_count
    posted_at        when the log was posted (UTC)
    channel_kind     "ticket" or "public"
    root_cause       the analyzer's verdict when it posted the card (culprit mod/jar, deepest exception, the
                     exception chain), or null
    uploader_text    the message the log was posted with
    before           up to 10 messages from the 30 minutes before the log, oldest first
    after            up to 10 messages from the 48 hours after it, oldest first, read when this zip was made
    after_available  false when the message or channel was deleted by then (after is empty)

Each message is {"who": "uploader" | "staff" | "member", "at": ISO time, "text": ...}; messages in "after" also have
"is_reply_to_log". Mentions read @user, @role and #channel. No Discord ids or names are included.
"""


@dataclass(frozen=True)
class Entry:
    message_id: int
    filename: str
    channel_id: int
    guild_id: int
    dmz_version: str
    notice_message_id: int | None
    excluded: bool
    created_at: datetime
    bundled_at: datetime | None

    @property
    def stem(self) -> str:
        return f"{self.message_id}-{self.filename}"

    @property
    def key(self) -> str:
        return file_key(self.filename)


_COLUMNS = (
    "message_id, filename, channel_id, guild_id, dmz_version, notice_message_id, excluded, created_at, bundled_at"
)


def _entry(row) -> Entry:
    return Entry(**dict(row))


# ── Names, text and files ─────────────────────────────────────────────────────

def safe_filename(name: str, names: Iterable[str] = ()) -> str:
    """The attachment name as stored: the log's account names masked, only [A-Za-z0-9._-], at most 100 chars."""
    for player in sorted(set(names), key=len, reverse=True):
        name = re.sub(re.escape(player), "player", name, flags=re.IGNORECASE)
    cleaned = _UNSAFE_CHARS_RE.sub("_", name).strip("._") or "log.txt"
    return cleaned[-100:]


def file_key(filename: str) -> str:
    """Short stand-in for a stored file name in custom ids (they hold 100 characters)."""
    return hashlib.sha1(filename.encode("utf-8")).hexdigest()[:10]


def version_key(version: str | None) -> str:
    return (version or "").strip()[:VERSION_MAX] or "unknown"


def log_path(message_id: int, filename: str) -> Path:
    return CORPUS_DIR / f"{message_id}-{filename}"


def context_path(message_id: int, filename: str) -> Path:
    return CORPUS_DIR / f"{message_id}-{filename}.json"


def scrub(text: str, names: Iterable[str] = ()) -> str:
    """Chat text for the corpus: mentions become @user/@role/#channel, personal details are masked, no live pings."""
    text = _USER_MENTION_RE.sub("@user", text or "")
    text = _ROLE_MENTION_RE.sub("@role", text)
    text = _CHANNEL_MENTION_RE.sub("#channel", text)
    return defuse_mentions(log_pages.redact(text, names))


def _name_hash(salt: str, name: str) -> str:
    return hashlib.sha256(f"{salt}:{name.lower()}".encode("utf-8")).hexdigest()


def hide_names(names: Iterable[str]) -> dict:
    """The account names as salted hashes, enough to mask them later without keeping them."""
    salt = secrets.token_hex(8)
    return {"salt": salt, "hashes": sorted({_name_hash(salt, name) for name in names})}


def mask_hidden_names(text: str, hidden: dict | None) -> str:
    if not hidden or not hidden.get("hashes"):
        return text
    salt, hashes = hidden["salt"], set(hidden["hashes"])
    return _NAME_TOKEN_RE.sub(lambda m: "<player>" if _name_hash(salt, m.group(0)) in hashes else m.group(0), text)


def who(author: Any, uploader_id: int, settings: Any) -> str:
    if getattr(author, "id", None) == uploader_id:
        return "uploader"
    if is_staff(author, settings=settings) or has_staff_rank(author, settings):
        return "staff"
    return "member"


def context_message(
    message: Any,
    *,
    uploader_id: int,
    settings: Any,
    names: Iterable[str] = (),
    hidden: dict | None = None,
    log_message_id: int | None = None,
) -> dict:
    """One chat message as the corpus keeps it; hidden masks names from hide_names, log_message_id adds
    is_reply_to_log (messages after the log)."""
    entry = {
        "who": who(message.author, uploader_id, settings),
        "at": message.created_at.isoformat(),
        "text": mask_hidden_names(scrub(message.content or "", names), hidden),
    }
    if log_message_id is not None:
        reference = getattr(message, "reference", None)
        entry["is_reply_to_log"] = getattr(reference, "message_id", None) == log_message_id
    return entry


def metadata(report: Any, *, filename: str, posted_at: datetime, channel_kind: str, names: Iterable[str] = ()) -> dict:
    """The analysis facts the notice card and the .zip show, taken when the log was analyzed."""
    cause = report.root_cause
    root_cause = None
    if cause is not None:
        root_cause = {
            "culprit_mod": cause.culprit_mod,
            "culprit_jar": cause.culprit_jar,
            "exception": log_pages.redact(cause.exception, names),
            "chain": [log_pages.redact(line, names) for line in cause.chain],
            "kind": cause.kind,
        }
    return {
        "file": filename,
        "dmz_version": version_key(report.dragonminez_version),
        "minecraft_version": report.mc_version,
        "forge_version": report.forge_version,
        "java_version": report.java_version,
        "error_count": len(report.errors),
        "posted_at": posted_at.isoformat(),
        "channel_kind": channel_kind,
        "root_cause": root_cause,
    }


def write_files(message_id: int, filename: str, redacted_log: str, context: dict) -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    log_path(message_id, filename).write_text(redacted_log, encoding="utf-8")
    context_path(message_id, filename).write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")


def read_context(message_id: int, filename: str) -> dict:
    return json.loads(context_path(message_id, filename).read_text(encoding="utf-8"))


def delete_files(keys: Iterable[tuple[int, str]]) -> None:
    for message_id, filename in keys:
        for path in (log_path(message_id, filename), context_path(message_id, filename)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.exception("Could not delete log corpus file", extra={"path": str(path)})


def purge_old_files(*, now: float | None = None) -> int:
    """Files past retention that no row points at any more (a crash between writing and recording)."""
    if not CORPUS_DIR.is_dir():
        return 0
    cutoff = (now or time.time()) - RETENTION_DAYS * 86400
    removed = 0
    for path in CORPUS_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            log.exception("Could not purge log corpus file", extra={"path": str(path)})
    return removed


# ── Zips ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BundleItem:
    """One entry's two files as they go in the zip."""

    folder: str
    stem: str
    log: bytes
    context: bytes

    def members(self) -> list[tuple[str, bytes]]:
        return [(f"{self.folder}/{self.stem}", self.log), (f"{self.folder}/{self.stem}.json", self.context)]


def _estimate(name: str, data: bytes) -> int:
    # Deflated size plus the local header and the central directory record, both of which carry the name.
    return len(zlib.compress(data, 6)) + 2 * len(name.encode("utf-8")) + 128


def plan_parts(items: Sequence[BundleItem], limit: int) -> list[list[BundleItem]]:
    """Items split into zips that each stay under limit bytes, in order. An item too big on its own gets a zip of
    its own (the upload then fails and nothing is marked)."""
    budget = max(limit - ZIP_MARGIN - _estimate("README.txt", README.encode("utf-8")), 1)
    parts: list[list[BundleItem]] = []
    size = 0
    for item in items:
        cost = sum(_estimate(name, data) for name, data in item.members())
        if parts and parts[-1] and size + cost <= budget:
            parts[-1].append(item)
            size += cost
        else:
            parts.append([item])
            size = cost
    return parts


def write_zip(path: Path, items: Sequence[BundleItem]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", README)
        for item in items:
            for name, data in item.members():
                archive.writestr(name, data)


def build_zips(items: Sequence[BundleItem], limit: int) -> list[tuple[Path, list[BundleItem]]]:
    """Temp zip files (the caller deletes them) with the items each holds."""
    out: list[tuple[Path, list[BundleItem]]] = []
    try:
        for part in plan_parts(items, limit):
            handle = tempfile.NamedTemporaryFile(prefix="log-corpus-", suffix=".zip", delete=False)
            handle.close()
            path = Path(handle.name)
            out.append((path, part))
            write_zip(path, part)
    except Exception:
        for path, _part in out:
            path.unlink(missing_ok=True)
        raise
    return out


def zip_name(version: str | None, part: int, parts: int, *, today: datetime) -> str:
    label = safe_filename(version) if version else "all"
    suffix = f"-part{part}" if parts > 1 else ""
    return f"dmz-logs-{label}-{today:%Y%m%d}{suffix}.zip"


# ── Database ──────────────────────────────────────────────────────────────────

async def insert_entry(message_id: int, filename: str, channel_id: int, guild_id: int, dmz_version: str) -> bool:
    """False when this message + file is already in the corpus."""
    pool = await get_pool()
    inserted = await pool.fetchval(
        "INSERT INTO log_corpus_entries (message_id, filename, channel_id, guild_id, dmz_version) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (message_id, filename) DO NOTHING RETURNING message_id",
        message_id, filename, channel_id, guild_id, version_key(dmz_version),
    )
    return inserted is not None


async def delete_entry(message_id: int, filename: str) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM log_corpus_entries WHERE message_id = $1 AND filename = $2", message_id, filename)


async def set_notice(message_id: int, filename: str, notice_message_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE log_corpus_entries SET notice_message_id = $3 WHERE message_id = $1 AND filename = $2",
        message_id, filename, notice_message_id,
    )


async def find_entry(message_id: int, key: str) -> Entry | None:
    pool = await get_pool()
    rows = await pool.fetch(f"SELECT {_COLUMNS} FROM log_corpus_entries WHERE message_id = $1", message_id)
    return next((entry for entry in map(_entry, rows) if entry.key == key), None)


async def set_excluded(message_id: int, filename: str, excluded: bool) -> Entry | None:
    """None once the entry is bundled or gone."""
    pool = await get_pool()
    row = await pool.fetchrow(
        f"UPDATE log_corpus_entries SET excluded = $3 WHERE message_id = $1 AND filename = $2 AND bundled_at IS NULL "
        f"RETURNING {_COLUMNS}",
        message_id, filename, excluded,
    )
    return _entry(row) if row else None


async def pending_count(dmz_version: str) -> int:
    pool = await get_pool()
    count = await pool.fetchval(
        "SELECT count(*) FROM log_corpus_entries WHERE dmz_version = $1 AND bundled_at IS NULL AND NOT excluded",
        dmz_version,
    )
    return int(count or 0)


async def claim_ping(dmz_version: str) -> bool:
    """True for the one caller that gets to ping for this version until its next bundle."""
    pool = await get_pool()
    claimed = await pool.fetchval(
        "INSERT INTO log_corpus_pings (dmz_version) VALUES ($1) ON CONFLICT (dmz_version) DO NOTHING "
        "RETURNING dmz_version",
        dmz_version,
    )
    return claimed is not None


async def clear_pings(versions: Iterable[str]) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM log_corpus_pings WHERE dmz_version = ANY($1::text[])", sorted(set(versions)))


async def pending_entries(dmz_version: str | None = None) -> list[Entry]:
    """Unbundled, not excluded entries, oldest first; every version when dmz_version is None."""
    pool = await get_pool()
    if dmz_version is None:
        rows = await pool.fetch(
            f"SELECT {_COLUMNS} FROM log_corpus_entries WHERE bundled_at IS NULL AND NOT excluded "
            "ORDER BY dmz_version, created_at"
        )
    else:
        rows = await pool.fetch(
            f"SELECT {_COLUMNS} FROM log_corpus_entries WHERE dmz_version = $1 AND bundled_at IS NULL AND NOT excluded "
            "ORDER BY created_at",
            dmz_version,
        )
    return [_entry(row) for row in rows]


async def mark_bundled(entries: Sequence[Entry]) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE log_corpus_entries SET bundled_at = now() "
        "WHERE (message_id, filename) IN (SELECT * FROM unnest($1::bigint[], $2::text[]))",
        [entry.message_id for entry in entries],
        [entry.filename for entry in entries],
    )


async def purge_expired() -> int:
    """Deletes rows older than RETENTION_DAYS (and the files of unbundled ones), plus stray old files and the pings of
    versions with nothing left to download. Returns how many rows went."""
    pool = await get_pool()
    rows = await pool.fetch(
        "DELETE FROM log_corpus_entries WHERE created_at < now() - make_interval(days => $1) "
        "RETURNING message_id, filename, bundled_at",
        RETENTION_DAYS,
    )
    unbundled = [(row["message_id"], row["filename"]) for row in rows if row["bundled_at"] is None]
    await asyncio.to_thread(delete_files, unbundled)
    await asyncio.to_thread(purge_old_files)
    await pool.execute(
        "DELETE FROM log_corpus_pings p WHERE NOT EXISTS (SELECT 1 FROM log_corpus_entries e "
        "WHERE e.dmz_version = p.dmz_version AND e.bundled_at IS NULL AND NOT e.excluded)"
    )
    return len(rows)
