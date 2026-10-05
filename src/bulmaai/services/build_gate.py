"""Build gate state (table build_requests, cogs/build_gate.py): a Java push asks staff-devs before the
dev-jar workflow runs. Rows are the durable state, so the auto-close sweep and the live progress tracker
both resume after a restart. Also holds the pure helpers (push parsing, signature check, progress math)."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bulmaai.database.db import get_pool

PENDING = "pending"
BUILDING = "building"
SUCCEEDED = "succeeded"
FAILED = "failed"
REJECTED = "rejected"
EXPIRED = "expired"
SUPERSEDED = "superseded"

NULL_SHA = "0" * 40
MAX_COMMITS_INPUT = 40
MAX_CHANGELOG_CHARS = 4000
MAX_INPUT_CHARS = 60000  # workflow_dispatch inputs are capped at 65535 chars


@dataclass(frozen=True, slots=True)
class PushInfo:
    branch: str
    head_sha: str
    pusher: str
    commits: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class BuildRequest:
    id: int
    repo: str
    branch: str
    head_sha: str
    pusher: str
    commits: tuple[dict[str, Any], ...]
    source: str
    status: str
    channel_id: int | None
    message_id: int | None
    expires_at: datetime
    decided_by: int | None
    decided_at: datetime | None
    run_id: int | None
    run_url: str | None
    created_at: datetime
    changelog: str | None = None
    preview_message_id: int | None = None

    @property
    def request_token(self) -> str:
        return f"dmz-build-{self.id}"


# --- pure helpers (unit tested directly) --------------------------------------------------------


def verify_signature(secret: str | None, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _commit(raw: dict[str, Any]) -> dict[str, Any]:
    lines = (raw.get("message") or "").splitlines()
    item = {
        "sha": raw["id"],
        "title": lines[0] if lines else "",
        "author": (raw.get("author") or {}).get("name") or "unknown",
        "url": raw.get("url") or "",
    }
    description = "\n".join(lines[1:]).strip()
    if description:
        item["description"] = description
    return item


def _touches_java(raw: dict[str, Any]) -> bool:
    paths = (*raw.get("added", ()), *raw.get("modified", ()), *raw.get("removed", ()))
    return any(str(path).endswith(".java") for path in paths)


def parse_push(
    payload: dict[str, Any], *, repo_full_name: str, ignore_branches: tuple[str, ...] = ("main",)
) -> PushInfo | None:
    """None when the push shouldn't prompt: other repo, tag, deleted branch, main, or no .java change."""
    if (payload.get("repository") or {}).get("full_name", "").lower() != repo_full_name.lower():
        return None
    ref = payload.get("ref") or ""
    if not ref.startswith("refs/heads/") or payload.get("deleted"):
        return None
    branch = ref.removeprefix("refs/heads/")
    head_sha = payload.get("after") or ""
    if branch in ignore_branches or not head_sha or head_sha == NULL_SHA:
        return None

    raw_commits = list(payload.get("commits") or [])
    head_commit = payload.get("head_commit")
    if not raw_commits and head_commit:
        raw_commits = [head_commit]
    # GitHub lists at most 20 commits; a bigger push can hide a .java change, so assume yes.
    truncated = len(raw_commits) >= 20 and len(raw_commits) < (payload.get("size") or 0)
    if not truncated and not any(_touches_java(c) for c in raw_commits):
        return None

    pusher = (payload.get("pusher") or {}).get("name") or (payload.get("sender") or {}).get("login") or "unknown"
    return PushInfo(branch=branch, head_sha=head_sha, pusher=pusher, commits=tuple(_commit(c) for c in raw_commits))


def merge_commits(older: tuple[dict[str, Any], ...], newer: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for commit in (*older, *newer):
        if commit["sha"] not in seen:
            seen.add(commit["sha"])
            merged.append(commit)
    return tuple(merged)


def commits_input(commits: tuple[dict[str, Any], ...]) -> str:
    """JSON for the workflow's commits_json input (the release webhook payload format), kept under the input cap."""
    trimmed = [dict(c) for c in commits[-MAX_COMMITS_INPUT:]]
    text = json.dumps(trimmed, separators=(",", ":"))
    if len(text) > MAX_INPUT_CHARS:
        for c in trimmed:
            c.pop("description", None)
        text = json.dumps(trimmed, separators=(",", ":"))
    return text if len(text) <= MAX_INPUT_CHARS else "[]"


SKIPPED_STEPS = ("Set up job", "Complete job")


def visible_steps(job: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps = (job or {}).get("steps") or []
    return [s for s in steps if s["name"] not in SKIPPED_STEPS and not s["name"].startswith("Post ")]


def progress_bar(done: int, total: int, width: int = 12) -> str:
    total = max(total, 1)
    filled = round(width * done / total)
    return f"{'▰' * filled}{'▱' * (width - filled)} {round(100 * done / total)}%"


def step_icon(step: dict[str, Any]) -> str:
    if step["status"] == "completed":
        return {"success": "✅", "skipped": "⏭️"}.get(step.get("conclusion"), "❌")
    return "🔄" if step["status"] == "in_progress" else "⬜"


# --- persistence --------------------------------------------------------------------------------


def _request(row) -> BuildRequest:
    commits = row["commits"]
    if isinstance(commits, str):
        commits = json.loads(commits)
    return BuildRequest(
        id=row["id"], repo=row["repo"], branch=row["branch"], head_sha=row["head_sha"], pusher=row["pusher"],
        commits=tuple(commits), source=row["source"], status=row["status"], channel_id=row["channel_id"],
        message_id=row["message_id"], expires_at=row["expires_at"], decided_by=row["decided_by"],
        decided_at=row["decided_at"], run_id=row["run_id"], run_url=row["run_url"], created_at=row["created_at"],
        changelog=row["changelog"], preview_message_id=row["preview_message_id"],
    )


async def create(
    *, repo: str, branch: str, head_sha: str, pusher: str, commits: tuple[dict[str, Any], ...],
    source: str, expires_at: datetime, changelog: str | None = None,
) -> BuildRequest:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO build_requests (repo, branch, head_sha, pusher, commits, source, expires_at, changelog)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) RETURNING *
        """,
        repo, branch, head_sha, pusher, json.dumps(list(commits)), source, expires_at, changelog,
    )
    return _request(row)


async def get(request_id: int) -> BuildRequest | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM build_requests WHERE id = $1", request_id)
    return _request(row) if row else None


async def set_message(request_id: int, channel_id: int, message_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE build_requests SET channel_id = $2, message_id = $3 WHERE id = $1", request_id, channel_id, message_id
    )


async def transition(
    request_id: int, new_status: str, *, from_status: str, by: int | None = None
) -> BuildRequest | None:
    """First transition wins: a staff click, the expiry sweep and a newer push race on the same row."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE build_requests SET status = $3, decided_by = COALESCE($4, decided_by), decided_at = now() "
        "WHERE id = $1 AND status = $2 RETURNING *",
        request_id, from_status, new_status, by,
    )
    return _request(row) if row else None


async def set_run(request_id: int, run_id: int, run_url: str) -> None:
    pool = await get_pool()
    await pool.execute("UPDATE build_requests SET run_id = $2, run_url = $3 WHERE id = $1", request_id, run_id, run_url)


def clean_changelog(text: str | None) -> str | None:
    cleaned = (text or "").strip()[:MAX_CHANGELOG_CHARS].strip()
    return cleaned or None


async def set_changelog(request_id: int, text: str | None) -> BuildRequest | None:
    """None when the request is no longer pending or building: the changelog locks once the build ends."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE build_requests SET changelog = $2 WHERE id = $1 AND status IN ('pending', 'building') RETURNING *",
        request_id, clean_changelog(text),
    )
    return _request(row) if row else None


async def set_preview_message(request_id: int, message_id: int) -> None:
    pool = await get_pool()
    await pool.execute("UPDATE build_requests SET preview_message_id = $2 WHERE id = $1", request_id, message_id)


async def changelog_for_commit(commit_sha: str) -> str | None:
    """The changelog staff attached to the build that produced the jar with this (short) commit sha."""
    pool = await get_pool()
    return await pool.fetchval(
        "SELECT changelog FROM build_requests WHERE changelog IS NOT NULL AND status IN ('building', 'succeeded') "
        "AND left(head_sha, char_length($1)) = $1 ORDER BY id DESC LIMIT 1",
        commit_sha,
    )


async def with_status(status: str) -> list[BuildRequest]:
    pool = await get_pool()
    rows = await pool.fetch("SELECT * FROM build_requests WHERE status = $1 ORDER BY id", status)
    return [_request(row) for row in rows]


async def due(now: datetime) -> list[BuildRequest]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM build_requests WHERE status = 'pending' AND expires_at <= $1 ORDER BY id", now
    )
    return [_request(row) for row in rows]


async def recent(repo: str, since: datetime, *, exclude_id: int) -> list[BuildRequest]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM build_requests WHERE repo = $1 AND created_at >= $2 AND id <> $3 ORDER BY id DESC",
        repo, since, exclude_id,
    )
    return [_request(row) for row in rows]
