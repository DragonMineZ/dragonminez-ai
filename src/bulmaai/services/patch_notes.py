import difflib
from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool


PATCH_NOTES_REPO = "dragonminez"
PATCH_NOTES_BRANCH = "claude/v2.1-main-patch-notes-JO139"
PATCH_NOTES_FILE_PATH = "PATCH_NOTES_2.1.md"
PATCH_NOTES_URL = (
    f"https://github.com/DragonMineZ/{PATCH_NOTES_REPO}"
    f"/blob/{PATCH_NOTES_BRANCH}/{PATCH_NOTES_FILE_PATH}"
)


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
