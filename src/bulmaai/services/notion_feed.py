"""The Notion Tracker mirrored into a staff forum (cogs/notion_feed): one post per item, kept in notion_posts.
Pure helpers read an item off a page; the row holds the last status/priority/title the post showed, so each webhook
compares the fetched page against it instead of trusting the event."""

from dataclasses import dataclass

from bulmaai.database.db import get_pool
from bulmaai.services import notion

DONE = notion.DONE_STATUSES  # ("Done", "Won't do"): the post is archived
DELETED = "Deleted"  # stored status of an item trashed in Notion; its post is archived and locked
P0 = "P0 Critical"
P0_TAG = "P0"
TAG_EMOJI = {
    "Mod": "🧩", "Website": "🌐", "Forum": "💬", "Community": "👥",
    "Inbox": "📥", "Backlog": "🗂️", "In progress": "🔨", "In review": "👀", "Done": "✅", "Won't do": "🚫",
    P0_TAG: "🚨",
}
NAME_LIMIT = 100  # Discord's thread name cap


@dataclass(frozen=True, slots=True)
class Post:
    page_id: str
    thread_id: int
    status: str | None
    priority: str | None
    title: str | None


def _key(page_id: str) -> str:
    return str(page_id or "").replace("-", "").lower()


def same_id(a: object, b: object) -> bool:
    """Notion ids with or without dashes, any case."""
    return bool(a and b) and _key(str(a)) == _key(str(b))


def item_from_page(page: dict) -> dict:
    properties = page.get("properties") or {}
    named = {name: notion.flatten(value) for name, value in properties.items()}
    key = next((notion.flatten(v) for v in properties.values() if v.get("type") == "unique_id"), None)
    assignees = next((notion.flatten(v) for v in properties.values() if v.get("type") == "people"), None) or []
    parent = page.get("parent") or {}
    return {
        "id": page.get("id"),
        "key": key if isinstance(key, str) else None,
        "title": notion.summarize(page)["title"].strip(),
        "url": page.get("url"),
        "type": named.get("Type"),
        "project": named.get("Project"),
        "status": named.get("Status"),
        "priority": named.get("Priority"),
        "assignees": [name for name in assignees if name],
        "parent": parent.get("data_source_id") if parent.get("type") == "data_source_id" else None,
        "in_trash": bool(page.get("in_trash") or page.get("archived")),
    }


def tags_for(item: dict) -> list[str]:
    names = [item.get("project"), item.get("status")]
    if item.get("priority") == P0:
        names.append(P0_TAG)
    return [name for name in names if name in TAG_EMOJI]


def post_name(item: dict) -> str:
    title = item.get("title") or "Untitled"
    name = f"{item['key']} · {title}" if item.get("key") else title
    return name if len(name) <= NAME_LIMIT else name[: NAME_LIMIT - 1].rstrip() + "…"


# --- notion_posts ------------------------------------------------------------------------------


async def get_post(page_id: str) -> Post | None:
    row = await (await get_pool()).fetchrow(
        "SELECT page_id, thread_id, status, priority, title FROM notion_posts WHERE page_id = $1", _key(page_id)
    )
    return Post(row["page_id"], row["thread_id"], row["status"], row["priority"], row["title"]) if row else None


async def save_post(page_id: str, thread_id: int, *, status: str | None, priority: str | None, title: str | None) -> None:
    await (await get_pool()).execute(
        """
        INSERT INTO notion_posts (page_id, thread_id, status, priority, title) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (page_id) DO UPDATE
        SET thread_id = $2, status = $3, priority = $4, title = $5, updated_at = now()
        """,
        _key(page_id),
        thread_id,
        status,
        priority,
        title,
    )


async def delete_post(page_id: str) -> None:
    await (await get_pool()).execute("DELETE FROM notion_posts WHERE page_id = $1", _key(page_id))
