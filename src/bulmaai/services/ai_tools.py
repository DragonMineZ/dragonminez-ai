"""Read-only data the support AI can see.

The same handlers serve two paths:
- escalation runs expose them as function tools (billed, the model decides what to look up);
- the free first pass gets `build_prefetch_lines` output injected as context instead.

Identity tools only ever receive the triggering user's id (forced in openai_client._hydrate_tool_args).
Outputs are minimized: no real names, Patreon ids, tier ids or billing dates.
"""

import asyncio
import json
import logging
import re
from typing import Any

from bulmaai.config import load_settings
from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services.bug_reports import list_bug_reports_by_reporter
from bulmaai.services.curseforge_state import get_curseforge_project_state
from bulmaai.services.dev_jar_download_records import has_completed_dev_jar_download
from bulmaai.services.dev_jar_downloads import parse_dev_jar_filename
from bulmaai.services.dev_jar_published_state import get_published_dev_jar_file_name
from bulmaai.services.patch_notes import build_patch_notes_url, get_patch_notes_state
from bulmaai.services.patreon_grants import (
    get_patreon_link,
    list_active_gifts_for_beneficiary,
    list_active_grants_for_owner,
)
from bulmaai.utils.permissions import has_any_allowed_role, has_patreon_access_role, is_admin

log = logging.getLogger(__name__)

PATCH_NOTES_EXCERPT_CHARS = 1500
IDENTITY_TOOLS = frozenset({"get_patreon_status", "get_user_bug_reports"})
ACCOUNT_TOPIC_RE = re.compile(
    r"(?i)\b(patreon|patron|beta|alpha|whitelist|allowlist|white[- ]?list|dev[- ]?jar|jar|tier|"
    r"access|acceso|acesso|gift|gifted|regalo|presente|role|rol|cargo|subscri\w*|suscri\w*|assin\w*)\b"
)
BUG_TOPIC_RE = re.compile(r"(?i)\b(bug|bugs|report|reported|issue|reporte|reportado|relat\w*|github)\b")
PATCH_NOTES_TOPIC_RE = re.compile(
    r"(?i)\b(patch|changelog|update|updated|new version|release|novedad\w*|cambios|atualiza\w*|novidade\w*)\b"
)


def _settings(bot: Any) -> Any:
    return getattr(bot, "settings", None) or load_settings()


def _find_member(bot: Any, user_id: int) -> Any:
    for guild in getattr(bot, "guilds", ()) or ():
        member = guild.get_member(user_id)
        if member is not None:
            return member
    return None


async def get_patreon_status(discord_user_id: str | None = None, _bot_context: Any = None) -> dict[str, Any]:
    user_id = int(discord_user_id or 0)
    settings = _settings(_bot_context)
    member = _find_member(_bot_context, user_id)
    link, grants, gifted, published = await asyncio.gather(
        get_patreon_link(user_id),
        list_active_grants_for_owner(user_id),
        list_active_gifts_for_beneficiary(user_id),
        get_published_dev_jar_file_name(),
    )
    can_download = bool(
        member is not None
        and (
            is_admin(member)
            or has_any_allowed_role(member, settings.dev_jar_patreon_role_ids)
            or has_any_allowed_role(member, settings.dev_jar_tester_role_ids)
        )
    )
    return {
        "in_discord_server": member is not None,
        "has_patreon_discord_role": bool(member is not None and has_patreon_access_role(member, settings=settings)),
        "patreon_account_linked_with_bot": link is not None,
        "patron_status": link.patron_status if link else None,
        "entitlement_active": bool(link and link.entitlement_active),
        "whitelist_entries": [
            {"minecraft_username": grant.minecraft_username, "kind": str(grant.kind)} for grant in grants
        ],
        "gifts_given": sum(1 for grant in grants if str(grant.kind) == "gift"),
        "gifted_access_received": [{"minecraft_username": grant.minecraft_username} for grant in gifted],
        "can_download_dev_jar": can_download,
        "current_dev_jar": published,
        "downloaded_current_dev_jar": bool(
            published and await has_completed_dev_jar_download(user_id, published)
        ),
    }


async def get_user_bug_reports(discord_user_id: str | None = None, _bot_context: Any = None) -> dict[str, Any]:
    reports = await list_bug_reports_by_reporter(int(discord_user_id or 0), limit=5)
    return {
        "bug_reports": [
            {
                "title": report.ai_title,
                "status": report.status,
                "github_issue": (
                    f"https://github.com/{report.repo}/issues/{report.issue_number}"
                    if report.repo and report.issue_number
                    else None
                ),
                "reported_at": report.created_at.date().isoformat() if report.created_at else None,
            }
            for report in reports
        ]
    }


async def search_known_issues(query: str = "", _bot_context: Any = None) -> dict[str, Any]:
    settings = _settings(_bot_context)
    service = GitHubService(
        auth=GitHubAppAuth(
            app_id=settings.GH_APP_ID,
            installation_id=settings.GH_INSTALLATION_ID,
            private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM,
        ),
        owner=settings.GITHUB_OWNER,
        repo=settings.bug_report_repo,
        base_branch=settings.GITHUB_BASE_BRANCH,
    )
    items = await service.search_issues(query.strip()[:200], per_page=8)
    return {
        "issues": [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "labels": [label.get("name") for label in item.get("labels") or [] if isinstance(label, dict)],
                "updated_at": (item.get("updated_at") or "")[:10] or None,
                "url": item.get("html_url"),
            }
            for item in items
            if "pull_request" not in item
        ][:5]
    }


async def get_latest_releases(include_patch_notes: bool = True, _bot_context: Any = None) -> dict[str, Any]:
    settings = _settings(_bot_context)
    curseforge, published, patch_notes = await asyncio.gather(
        get_curseforge_project_state(settings.curseforge_project_id),
        get_published_dev_jar_file_name(),
        get_patch_notes_state(settings.patch_notes_branch, settings.patch_notes_file_path),
    )
    dev_jar = None
    if published:
        try:
            artifact = parse_dev_jar_filename(published)
            dev_jar = {"version": artifact.version, "commit": artifact.commit_sha[:7]}
        except ValueError:
            dev_jar = {"file": published}
    result: dict[str, Any] = {
        "latest_public_release": (
            {
                "file": curseforge.last_processed_file_name,
                "url": curseforge.last_processed_file_url,
                "released_at": (
                    curseforge.last_processed_at.date().isoformat() if curseforge.last_processed_at else None
                ),
            }
            if curseforge
            else None
        ),
        "current_dev_jar_for_patrons": dev_jar,
        "patch_notes_url": build_patch_notes_url(
            settings.patch_notes_repo, settings.patch_notes_branch, settings.patch_notes_file_path
        ),
    }
    if include_patch_notes and patch_notes is not None:
        result["patch_notes_excerpt"] = patch_notes.content[:PATCH_NOTES_EXCERPT_CHARS]
    return result


def _function_schema(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {"type": "object", "properties": properties, "additionalProperties": False},
    }


_USER_ID_PROPERTY = {
    "type": ["string", "null"],
    "description": "Leave null: the requester is used. Only staff in DMs may look up someone else.",
}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_patreon_status": _function_schema(
        "get_patreon_status",
        "The requester's Patreon link, Discord patron role, beta whitelist entries, gifts and dev jar "
        "access/download state. Use for any Patreon, whitelist, beta/alpha access or dev jar question.",
        {"discord_user_id": _USER_ID_PROPERTY},
    ),
    "get_user_bug_reports": _function_schema(
        "get_user_bug_reports",
        "The requester's last 5 bug reports with status and linked GitHub issue.",
        {"discord_user_id": _USER_ID_PROPERTY},
    ),
    "search_known_issues": _function_schema(
        "search_known_issues",
        "Search DragonMineZ GitHub issues (open and closed) for a crash/bug symptom to see if it is known or fixed.",
        {"query": {"type": "string", "description": "A few keywords, e.g. an exception name or feature."}},
    ),
    "get_latest_releases": _function_schema(
        "get_latest_releases",
        "Latest public CurseForge release, current patron dev jar version, and the patch notes.",
        {"include_patch_notes": {"type": "boolean", "description": "Include a patch notes excerpt."}},
    ),
}
TOOL_FUNCS = {
    "get_patreon_status": get_patreon_status,
    "get_user_bug_reports": get_user_bug_reports,
    "search_known_issues": search_known_issues,
    "get_latest_releases": get_latest_releases,
}
SUPPORT_TOOL_NAMES = list(TOOL_SCHEMAS)


async def _labeled(label: str, coro: Any) -> str | None:
    try:
        return f"{label}: {json.dumps(await coro, ensure_ascii=False, default=str)}"
    except Exception:
        log.exception("Support prefetch failed", extra={"event": "support_prefetch_failed", "label": label})
        return None


async def build_prefetch_lines(*, bot: Any, user_id: int, text: str) -> list[str]:
    """Data for the tool-free first pass. Account data is only fetched when the conversation is about it."""
    # ponytail: keyword routing; the escalation pass has real tools for anything this misses.
    jobs = [
        _labeled(
            "releases",
            get_latest_releases(include_patch_notes=bool(PATCH_NOTES_TOPIC_RE.search(text)), _bot_context=bot),
        )
    ]
    if ACCOUNT_TOPIC_RE.search(text):
        jobs.append(_labeled("requester_account", get_patreon_status(str(user_id), _bot_context=bot)))
    if BUG_TOPIC_RE.search(text):
        jobs.append(_labeled("requester_bug_reports", get_user_bug_reports(str(user_id), _bot_context=bot)))
    return [line for line in await asyncio.gather(*jobs) if line]
