"""Bug reports mirrored into the team's Notion Tracker, one way (bot → Notion). The bot only creates the item and later
sets its GitHub link and final status; triage, owners and releases are done by people in Notion."""

import logging

from bulmaai.services import http

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
TEXT_LIMIT = 2000  # Notion's cap per rich text item
PRIORITY = {"critical": "P0 Critical", "high": "P1 High", "medium": "P2 Normal", "low": "P3 Low"}
# bug_reports.status -> Tracker status; other statuses leave the item alone (people move it in Notion).
STATUS = {"resolved": "Done", "dismissed": "Won't do"}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"}


def _text(value: str) -> list[dict]:
    return [{"type": "text", "text": {"content": value[:TEXT_LIMIT]}}]


def new_item(data_source_id: str, *, title: str, summary: str, severity: str, reporter: str, thread_url: str) -> dict:
    """The create-page body: a Bug in the Mod project, Inbox, priority from the AI's severity."""
    return {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "icon": {"type": "emoji", "emoji": "🐛"},
        "properties": {
            "Name": {"title": _text(title or "Bug report")},
            "Type": {"select": {"name": "Bug"}},
            "Project": {"select": {"name": "Mod"}},
            "Status": {"status": {"name": "Inbox"}},
            "Priority": {"select": {"name": PRIORITY.get(severity, "P2 Normal")}},
            "Reported by": {"rich_text": _text(reporter)},
        },
        "children": [
            {"type": "paragraph", "paragraph": {"rich_text": _text(summary or "No summary.")}},
            {"type": "bookmark", "bookmark": {"url": thread_url}},
        ],
    }


def item_update(*, status: str | None = None, github: str | None = None) -> dict:
    properties: dict = {}
    if status:
        properties["Status"] = {"status": {"name": status}}
    if github:
        properties["GitHub"] = {"url": github}
    return {"properties": properties}


async def create_item(token: str, data_source_id: str, **fields) -> str | None:
    """Creates the Tracker item and returns its page id, or None when Notion refused it (logged)."""
    response = await http.request(
        "POST", f"{API}/pages", headers=_headers(token), json=new_item(data_source_id, **fields), timeout=20
    )
    if response.status_code >= 400:
        log.warning("Notion refused the bug report item (%s): %s", response.status_code, response.text[:300])
        return None
    return response.json().get("id")


async def update_item(token: str, page_id: str, *, status: str | None = None, github: str | None = None) -> None:
    body = item_update(status=status, github=github)
    if not body["properties"]:
        return
    response = await http.request("PATCH", f"{API}/pages/{page_id}", headers=_headers(token), json=body, timeout=20)
    if response.status_code >= 400:
        log.warning("Notion refused the update of %s (%s): %s", page_id, response.status_code, response.text[:300])
