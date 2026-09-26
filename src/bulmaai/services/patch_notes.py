import difflib
import re
from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool


def build_patch_notes_url(repo: str, branch: str, file_path: str) -> str:
    """Build the human-facing GitHub blob URL for the patch notes file.

    Repo/branch/file path are runtime settings (they move per release), so the
    URL is derived on demand rather than pinned to a module constant.
    """
    return f"https://github.com/DragonMineZ/{repo}/blob/{branch}/{file_path}"


_VERSION_RE = re.compile(r"v(\d+(?:\.\d+)*)", re.IGNORECASE)


def _version_key(path: str) -> tuple[int, ...]:
    match = _VERSION_RE.search(path.rsplit("/", 1)[-1])
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def pick_latest_patch_notes(paths: list[str]) -> str | None:
    """Pick the highest-versioned .md file (PATCH_NOTES-v2.2.md beats PATCH_NOTES-v2.1.1.md)."""
    return max((p for p in paths if p.lower().endswith(".md")), key=_version_key, default=None)


@dataclass(frozen=True, slots=True)
class PatchNotesState:
    branch: str
    file_path: str
    content_sha: str
    content: str
    updated_at: datetime | None = None


async def get_patch_notes_state(branch: str, file_path: str) -> PatchNotesState | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT branch, file_path, content_sha, content, updated_at
            FROM patch_notes_state
            WHERE branch = $1
              AND file_path = $2
            """,
            branch,
            file_path,
        )
    if row is None:
        return None
    return PatchNotesState(
        branch=str(row["branch"]),
        file_path=str(row["file_path"]),
        content_sha=str(row["content_sha"]),
        content=str(row["content"]),
        updated_at=row["updated_at"],
    )


async def upsert_patch_notes_state(state: PatchNotesState) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO patch_notes_state (branch, file_path, content_sha, content, updated_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (branch, file_path)
            DO UPDATE SET
                content_sha = EXCLUDED.content_sha,
                content = EXCLUDED.content,
                updated_at = now()
            """,
            state.branch,
            state.file_path,
            state.content_sha,
            state.content,
        )


def summarize_patch_notes_update(
    old_content: str,
    new_content: str,
    *,
    max_lines: int = 10,
    max_chars: int = 900,
) -> str:
    """Return the lines added since the last revision, trimmed to fit an embed field."""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    added = [
        line.strip()
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
        if tag in ("insert", "replace")
        for line in new_lines[j1:j2]
        if line.strip()
    ]
    if not added:
        return "The patch notes were revised; open the link for the full document."

    shown: list[str] = []
    used_chars = 0
    for line in added:
        if len(shown) >= max_lines:
            break
        entry = line if len(line) <= 200 else line[:197].rstrip() + "..."
        if used_chars + len(entry) + 1 > max_chars:
            break
        shown.append(entry)
        used_chars += len(entry) + 1

    remaining = len(added) - len(shown)
    if remaining > 0:
        shown.append(f"...and {remaining} more new line{'s' if remaining != 1 else ''}.")
    return "\n".join(shown)
