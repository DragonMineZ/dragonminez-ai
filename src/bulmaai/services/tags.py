"""Staff-written canned text answers, grouped by category, shown with /tag (cogs/tags.py)."""

import re
from dataclasses import dataclass

from bulmaai.database.db import get_pool

CATEGORIES = ("api", "error", "mixin", "other")
NAME_MAX = 40
BODY_MAX = 3000
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]*")


@dataclass(frozen=True, slots=True)
class Tag:
    category: str
    name: str
    body: str


def clean_name(name: str) -> str:
    """Lowercase name, ValueError when it isn't a short slug (letters, digits, - _ .)."""
    name = (name or "").strip().lower().replace(" ", "-")
    if len(name) > NAME_MAX or not _NAME.fullmatch(name):
        raise ValueError(f"Names are 1-{NAME_MAX} letters, digits, - _ or .")
    return name


def clean_body(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise ValueError("The text can't be empty.")
    if len(body) > BODY_MAX:
        raise ValueError(f"Text is too long ({len(body)}/{BODY_MAX} characters).")
    return body


async def get(category: str, name: str) -> Tag | None:
    row = await (await get_pool()).fetchrow(
        "SELECT category, name, body FROM tags WHERE category = $1 AND name = $2", category, name.strip().lower()
    )
    return Tag(row["category"], row["name"], row["body"]) if row else None


async def save(category: str, name: str, body: str, by: int) -> Tag:
    if category not in CATEGORIES:
        raise ValueError("Unknown category.")
    name, body = clean_name(name), clean_body(body)
    await (await get_pool()).execute(
        """
        INSERT INTO tags (category, name, body, updated_by) VALUES ($1, $2, $3, $4)
        ON CONFLICT (category, name) DO UPDATE SET body = $3, updated_by = $4, updated_at = now()
        """,
        category,
        name,
        body,
        by,
    )
    return Tag(category, name, body)


async def remove(category: str, name: str) -> bool:
    status = await (await get_pool()).execute(
        "DELETE FROM tags WHERE category = $1 AND name = $2", category, name.strip().lower()
    )
    return status.endswith(" 1")


async def names(category: str | None = None, prefix: str = "", limit: int = 25) -> list[tuple[str, str]]:
    """(category, name) pairs, by category then name; prefix filters names."""
    rows = await (await get_pool()).fetch(
        """
        SELECT category, name FROM tags
        WHERE ($1::text IS NULL OR category = $1) AND name LIKE $2 || '%'
        ORDER BY category, name LIMIT $3
        """,
        category,
        (prefix or "").strip().lower().replace("%", "").replace("_", r"\_"),
        limit,
    )
    return [(row["category"], row["name"]) for row in rows]
