"""The team's Notion workspace over its REST API, with the internal integration's token (env NOTION_TOKEN). It sees only
the pages shared with that integration (share DMZ HQ and everything under it inherits).

Two users: bug reports staff confirmed (Create issue) become Tracker items (cogs/bug_reports), and Staff Bulma's notion_*
tools (services/staff_tools) read and, after the staff member's confirm click, write. Pushes are never logged here."""

import logging
import re
from typing import Any

from bulmaai.services import http

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
TEXT_LIMIT = 2000  # Notion's cap per rich text item
PRIORITY = {"critical": "P0 Critical", "high": "P1 High", "medium": "P2 Normal", "low": "P3 Low"}
# bug_reports.status -> Tracker status; other statuses leave the item alone (people move it in Notion).
STATUS = {"resolved": "Done", "dismissed": "Won't do"}
# Tracker options, so tool input is checked before Notion sees it.
TRACKER_STATUSES = ("Inbox", "Backlog", "In progress", "In review", "Done", "Won't do")
TRACKER_TYPES = ("Bug", "Feature", "Task", "Idea", "Content")
TRACKER_PROJECTS = ("Mod", "Bot", "Website", "Forum", "Community")
TRACKER_PRIORITIES = ("P0 Critical", "P1 High", "P2 Normal", "P3 Low")
DONE_STATUSES = ("Done", "Won't do")
_PAGE_ID = re.compile(r"([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})", re.I)
_ITEM_ID = re.compile(r"^(?:dmz-?)?(\d+)$", re.I)


class NotionError(Exception):
    """Notion refused or failed; the message is safe to show staff."""


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"}


def _text(value: str) -> list[dict]:
    return [{"type": "text", "text": {"content": value[:TEXT_LIMIT]}}]


async def _call(token: str, method: str, path: str, body: dict | None = None) -> dict:
    response = await http.request(method, f"{API}{path}", headers=_headers(token), json=body, timeout=20)
    if response.status_code >= 400:
        try:
            message = response.json().get("message") or response.text
        except ValueError:
            message = response.text
        log.warning("Notion %s %s refused (%s): %s", method, path, response.status_code, message[:300])
        if response.status_code == 404:
            raise NotionError("Notion can't find that page, or it isn't shared with the BulmaAI integration.")
        raise NotionError(f"Notion refused ({response.status_code}): {message[:200]}")
    return response.json()


# --- reading -----------------------------------------------------------------------------------


def _plain(rich: list[dict] | None) -> str:
    return "".join(part.get("plain_text", "") for part in rich or [])


def flatten(value: dict) -> Any:
    """One Notion property value as plain JSON (names, text, dates)."""
    kind = value.get("type")
    data = value.get(kind)
    if kind in ("title", "rich_text"):
        return _plain(data)
    if kind in ("select", "status"):
        return (data or {}).get("name")
    if kind == "multi_select":
        return [option.get("name") for option in data or []]
    if kind == "date":
        return (data or {}).get("start")
    if kind == "people":
        return [person.get("name") for person in data or []]
    if kind == "unique_id":
        return f"{data.get('prefix')}-{data.get('number')}" if data and data.get("prefix") else (data or {}).get("number")
    if kind == "formula":
        return (data or {}).get((data or {}).get("type"))
    if kind == "relation":
        return len(data or [])
    if kind in ("url", "number", "checkbox", "email", "created_time", "last_edited_time"):
        return data
    return None


def _title(obj: dict) -> str:
    if isinstance(obj.get("title"), list):  # a data source
        return _plain(obj["title"])
    for value in (obj.get("properties") or {}).values():
        if value.get("type") == "title":
            return _plain(value.get("title"))
    return ""


def summarize(page: dict) -> dict:
    properties = {name: flatten(value) for name, value in (page.get("properties") or {}).items()}
    return {
        "id": page.get("id"),
        "title": _title(page),
        "url": page.get("url"),
        "properties": {name: value for name, value in properties.items() if value not in (None, "", [])},
    }


async def search(token: str, query: str, *, limit: int = 10) -> list[dict]:
    data = await _call(token, "POST", "/search", {"query": query[:200], "page_size": limit})
    return [
        {"id": obj.get("id"), "kind": obj.get("object"), "title": _title(obj), "url": obj.get("url")}
        for obj in data.get("results", [])
    ]


def tracker_filter(*, status: str | None = None, project: str | None = None, text: str | None = None,
                   include_done: bool = False) -> dict | None:
    parts: list[dict] = []
    if status:
        parts.append({"property": "Status", "status": {"equals": status}})
    elif not include_done:
        parts.extend({"property": "Status", "status": {"does_not_equal": name}} for name in DONE_STATUSES)
    if project:
        parts.append({"property": "Project", "select": {"equals": project}})
    if text:
        parts.append({"property": "Name", "title": {"contains": text[:100]}})
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"and": parts}


async def query_tracker(token: str, data_source_id: str, *, limit: int = 15, **filters) -> list[dict]:
    body: dict = {"page_size": limit, "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}]}
    if (condition := tracker_filter(**filters)) is not None:
        body["filter"] = condition
    data = await _call(token, "POST", f"/data_sources/{data_source_id}/query", body)
    return [summarize(page) for page in data.get("results", [])]


async def resolve_page(token: str, data_source_id: str, raw: Any) -> str:
    """A page id from a Notion link or id, or a Tracker item from DMZ-12 / 12."""
    text = str(raw or "").strip()
    if match := _PAGE_ID.search(text):
        return "-".join(match.groups()).lower()
    if match := _ITEM_ID.match(text):
        data = await _call(token, "POST", f"/data_sources/{data_source_id}/query", {
            "filter": {"property": "ID", "unique_id": {"equals": int(match.group(1))}}, "page_size": 1,
        })
        if data.get("results"):
            return data["results"][0]["id"]
        raise NotionError(f"There's no Tracker item DMZ-{match.group(1)}.")
    raise NotionError("Give a Notion link, a page id or a Tracker id like DMZ-12.")


async def read_page(token: str, page_id: str, *, block_limit: int = 50) -> dict:
    page = summarize(await _call(token, "GET", f"/pages/{page_id}"))
    blocks = await _call(token, "GET", f"/blocks/{page_id}/children?page_size={block_limit}")
    lines = []
    for block in blocks.get("results", []):
        kind = block.get("type")
        data = block.get(kind) or {}
        text = _plain(data.get("rich_text"))
        if kind == "to_do":
            text = ("[x] " if data.get("checked") else "[ ] ") + text
        elif kind in ("child_page", "child_database"):
            text = f"({kind.replace('child_', '')}) {data.get('title', '')}"
        if text:
            lines.append(text)
    page["content"] = "\n".join(lines)[:6000]
    return page


# --- writing -----------------------------------------------------------------------------------


def new_item(data_source_id: str, *, title: str, summary: str = "", item_type: str = "Task", project: str | None = None,
             priority: str | None = None, reporter: str = "", github: str | None = None, link: str | None = None) -> dict:
    """A create-page body for a Tracker item in Inbox."""
    properties: dict = {
        "Name": {"title": _text(title or "Untitled")},
        "Type": {"select": {"name": item_type}},
        "Status": {"status": {"name": "Inbox"}},
    }
    if project:
        properties["Project"] = {"select": {"name": project}}
    if priority:
        properties["Priority"] = {"select": {"name": priority}}
    if reporter:
        properties["Reported by"] = {"rich_text": _text(reporter)}
    if github:
        properties["GitHub"] = {"url": github}
    children: list[dict] = []
    if summary:
        children.append({"type": "paragraph", "paragraph": {"rich_text": _text(summary)}})
    if link:
        children.append({"type": "bookmark", "bookmark": {"url": link}})
    body: dict = {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties}
    if children:
        body["children"] = children
    return body


def bug_item(data_source_id: str, *, title: str, summary: str, severity: str, reporter: str, thread_url: str,
             github: str | None) -> dict:
    """A confirmed bug report: a Mod Bug, priority from the AI's severity."""
    body = new_item(
        data_source_id, title=title or "Bug report", summary=summary or "No summary.", item_type="Bug", project="Mod",
        priority=PRIORITY.get(severity, "P2 Normal"), reporter=reporter, github=github, link=thread_url,
    )
    body["icon"] = {"type": "emoji", "emoji": "🐛"}
    return body


def item_update(*, status: str | None = None, priority: str | None = None, title: str | None = None,
                github: str | None = None) -> dict:
    properties: dict = {}
    if status:
        properties["Status"] = {"status": {"name": status}}
    if priority:
        properties["Priority"] = {"select": {"name": priority}}
    if title:
        properties["Name"] = {"title": _text(title)}
    if github:
        properties["GitHub"] = {"url": github}
    return {"properties": properties}


async def create_page(token: str, body: dict) -> dict:
    page = await _call(token, "POST", "/pages", body)
    return {"id": page.get("id"), "url": page.get("url")}


async def update_page(token: str, page_id: str, **changes) -> dict:
    body = item_update(**changes)
    if not body["properties"]:
        raise NotionError("Nothing to change.")
    page = await _call(token, "PATCH", f"/pages/{page_id}", body)
    return {"id": page.get("id"), "url": page.get("url")}


async def append_text(token: str, page_id: str, text: str) -> None:
    paragraphs = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()][:20]
    children = [{"type": "paragraph", "paragraph": {"rich_text": _text(chunk)}} for chunk in paragraphs]
    await _call(token, "PATCH", f"/blocks/{page_id}/children", {"children": children})


async def comment(token: str, page_id: str, text: str) -> None:
    await _call(token, "POST", "/comments", {"parent": {"page_id": page_id}, "rich_text": _text(text)})
