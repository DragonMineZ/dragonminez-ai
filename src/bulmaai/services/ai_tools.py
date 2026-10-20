"""Read-only data the support AI can see, plus ask_requester and offer_action (controls under the reply).

The same handlers serve two paths:
- support runs expose them as Claude client tools (the model decides what to look up);
- every run also gets `build_prefetch_lines` output injected as context up front.

Identity tools only ever receive the triggering user's id (forced in ai_client._hydrate_tool_args).
Outputs are minimized: no real names, Patreon ids, tier ids or billing dates.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from bulmaai.config import load_settings
from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services import mod_source
from bulmaai.services.bug_reports import list_bug_reports_by_reporter
from bulmaai.services.curseforge_state import get_curseforge_project_state
from bulmaai.services.dev_jar_download_records import has_completed_dev_jar_download
from bulmaai.services.dev_jar_downloads import parse_dev_jar_filename
from bulmaai.services.dev_jar_published_state import get_published_dev_jar_file_name, get_published_dev_jar_state
from bulmaai.services.patch_notes import build_patch_notes_url, get_patch_notes_state
from bulmaai.services.patreon_grants import (
    get_patreon_link,
    list_active_gifts_for_beneficiary,
    list_active_grants_for_owner,
)
from bulmaai.ui import ai_asks
from bulmaai.ui.ticket_views import CATEGORIES
from bulmaai.utils.permissions import has_any_allowed_role, has_patreon_access_role, is_admin

log = logging.getLogger(__name__)

ASK_TOOL = "ask_requester"
ACTION_TOOL = "offer_action"
CONTROL_TOOLS = (ASK_TOOL, ACTION_TOOL)  # local, no lookup: they only attach controls to the reply
PATCH_NOTES_EXCERPT_CHARS = 1500
IDENTITY_TOOLS = frozenset({"get_patreon_status", "get_user_bug_reports", "search_known_issues", "get_latest_releases"})
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


def _is_patron(bot: Any, settings: Any, discord_user_id: str | None) -> bool:
    """Patreon access role or tester role: the people who have the dev jar."""
    member = _find_member(bot, int(discord_user_id or 0))
    return bool(
        member is not None
        and (
            has_patreon_access_role(member, settings=settings)
            or has_any_allowed_role(member, settings.dev_jar_tester_role_ids)
        )
    )


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
        "current_dev_jar": published if can_download else None,
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


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


async def _release_times(settings: Any) -> tuple[datetime, tuple[str, datetime] | None] | None:
    """(public release time, (dev jar file, published_at) or None), or None when unknown."""
    try:
        curseforge, dev_jar = await asyncio.gather(
            get_curseforge_project_state(settings.curseforge_project_id),
            get_published_dev_jar_state(),
        )
    except Exception:
        log.exception("Known-issue release lookup failed", extra={"event": "known_issues_release_lookup_failed"})
        return None
    if curseforge is None or curseforge.last_processed_at is None:
        return None
    return curseforge.last_processed_at, dev_jar


def _fix_status(
    item: dict[str, Any], times: tuple[datetime, tuple[str, datetime] | None], is_patron: bool
) -> dict[str, Any]:
    if item.get("state") != "closed":
        return {}
    if item.get("state_reason") != "completed":
        return {"fix": "not_fixed"}
    closed_at = _parse_time(item.get("closed_at"))
    if closed_at is None:
        return {}
    # ponytail: date-based (closed_at vs release/publish time), not commit ancestry; use the compare API on the closing commit if it misreports.
    public_at, dev_jar = times
    if closed_at <= public_at:
        return {"fix": "in_public_release"}
    if is_patron and dev_jar is not None and closed_at <= dev_jar[1]:
        fix: dict[str, Any] = {"fix": "in_dev_jar"}
        try:
            fix["dev_jar_version"] = parse_dev_jar_filename(dev_jar[0]).version
        except ValueError:
            pass
        return fix
    return {"fix": "pending_release"}


async def search_known_issues(
    query: str = "", discord_user_id: str | None = None, _bot_context: Any = None
) -> dict[str, Any]:
    settings = _settings(_bot_context)
    is_patron = _is_patron(_bot_context, settings, discord_user_id)
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
    items, times = await asyncio.gather(
        service.search_issues(query.strip()[:200], per_page=8),
        _release_times(settings),
    )
    return {
        "issues": [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "labels": [label.get("name") for label in item.get("labels") or [] if isinstance(label, dict)],
                "updated_at": (item.get("updated_at") or "")[:10] or None,
                "url": item.get("html_url"),
                **(_fix_status(item, times, is_patron) if times is not None else {}),
            }
            for item in items
            if "pull_request" not in item
        ][:5]
    }


async def get_latest_releases(
    include_patch_notes: bool = True, discord_user_id: str | None = None, _bot_context: Any = None
) -> dict[str, Any]:
    """The dev jar is shown only to patrons and testers: everyone else only has the public release."""
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
        "patch_notes_url": build_patch_notes_url(
            settings.patch_notes_repo, settings.patch_notes_branch, settings.patch_notes_file_path
        ),
    }
    if _is_patron(_bot_context, settings, discord_user_id):
        result["current_dev_jar_for_patrons"] = dev_jar
    if include_patch_notes and patch_notes is not None:
        result["patch_notes_excerpt"] = patch_notes.content[:PATCH_NOTES_EXCERPT_CHARS]
    return result


async def list_mod_files(area: str = "", folder: str | None = None, _bot_context: Any = None) -> dict[str, Any]:
    """The mod's own allowlisted files of one area (services/mod_source), on mod_source_branch."""
    return await mod_source.list_files(_settings(_bot_context), area, folder)


async def read_mod_file(
    path: str = "", query: str | None = None, start_line: int | None = None, _bot_context: Any = None
) -> dict[str, Any]:
    """Numbered lines of one allowlisted mod file: around query's matches, or from start_line."""
    return await mod_source.read_file(_settings(_bot_context), path, query, start_line)


async def ask_requester(_bot_context: Any = None, **spec: Any) -> dict[str, Any]:
    """Local, no lookup: validates the controls (ui/ai_asks.validate_ask). ai_client hands the last valid spec to the
    reply that gets sent; it never suppresses the reply."""
    return ai_asks.validate_ask(spec, _settings(_bot_context))


async def offer_action(_bot_context: Any = None, channel_kind: str | None = None, **spec: Any) -> dict[str, Any]:
    """Local, no lookup: validates the button (ui/ai_asks.validate_action). channel_kind is set by
    ai_client._hydrate_tool_args from Discord; nothing runs until the requester clicks."""
    return ai_asks.validate_action(spec, _settings(_bot_context), channel_kind)


def _function_schema(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {"type": "object", "properties": properties, "additionalProperties": False},
    }


# The ticket panel's categories, in English: "bug (Game-Breaking Bug: Crashes, corrupted worlds), ...".
_TICKET_CATEGORIES = ", ".join(
    f"{key} ({category.name}: {category.description.split(' | ')[0]})" for key, category in CATEGORIES.items()
)

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
        "Search DragonMineZ GitHub issues (open and closed) for a crash/bug symptom to see if it is known or fixed. "
        "On closed issues, `fix` says where the fix is available to this requester: in_public_release (update "
        "from CurseForge), in_dev_jar (patron dev jar, with dev_jar_version), pending_release (fixed, coming in "
        "the next release), not_fixed (closed without a fix). Never mention or promise a dev jar unless "
        "fix is in_dev_jar.",
        {
            "query": {"type": "string", "description": "A few keywords, e.g. an exception name or feature."},
            "discord_user_id": _USER_ID_PROPERTY,
        },
    ),
    "get_latest_releases": _function_schema(
        "get_latest_releases",
        "Latest public CurseForge release and the patch notes; the current dev jar version only when the "
        "requester is a patron. Never mention a dev jar version that isn't in this output.",
        {
            "include_patch_notes": {"type": "boolean", "description": "Include a patch notes excerpt."},
            "discord_user_id": _USER_ID_PROPERTY,
        },
    ),
    "list_mod_files": _function_schema(
        "list_mod_files",
        "List DragonMineZ's own source files that read_mod_file can read. Areas: config (the Java config classes: "
        "every option with its current default; ConfigManager says which file each class is saved to), "
        "defaults (the config JSON files the mod ships as its upgrade baseline: the same files, layout and keys as a "
        "player's config/dragonminez/<file>.json, e.g. general-server.json, combat.json, races/<race>/stats.json, "
        "forms/*.json; values can be a version old, the config classes hold the current defaults), commands (the "
        "server commands with their syntax, DMZPermissions with every permission node, DMZServer), lang (en_us.json: "
        "every in-game text and message by key), quests (the quest, saga and side-quest JSON files as shipped, in "
        "the layout of a world's dragonminez folder). A listing that is cut short shows folders with their file "
        "counts: pass one as folder to list it.",
        {
            "area": {"type": "string", "enum": list(mod_source.AREAS)},
            "folder": {
                "type": ["string", "null"],
                "description": "Null for the whole area, or a folder from a cut-short listing's folders.",
            },
        },
    ),
    "read_mod_file": _function_schema(
        "read_mod_file",
        "Read one DragonMineZ source file from list_mod_files (no other paths), as numbered lines. With query: "
        f"only the lines containing it (case-insensitive) with {mod_source.CONTEXT_LINES} lines around each; "
        "without: the file from start_line. About 6000 characters at most; truncated means there is more (read on "
        "from a later start_line, or narrow the query). A player's config/dragonminez/<file>.json path reads the "
        "shipped file of the same name. Use it for technical config, command and permission questions instead of "
        "guessing, and quote the exact key, value, command or permission node; never paste code.",
        {
            "path": {"type": "string", "description": "A path exactly as list_mod_files gave it."},
            "query": {
                "type": ["string", "null"],
                "description": f"A config key, command, permission or text to find, up to {mod_source.QUERY_MAX} "
                "characters; null reads a window.",
            },
            "start_line": {
                "type": ["integer", "null"],
                "description": "Without query: the first line to read (1 = the top). Null: 1.",
            },
        },
    ),
    # Not strict: each kind fills only its own fields; ui/ai_asks validates every call (errors go back to the model).
    ASK_TOOL: {
        "type": "function",
        "name": ASK_TOOL,
        "description": (
            "Show answer controls under your reply, so the requester answers your question with a click instead of "
            "typing. kind choices: 2-5 short answers as buttons. kind select: one dropdown of 2-25 options, for longer "
            "lists (versions from get_latest_releases, launchers). kind form: a Fill in button that opens a form of "
            "1-5 fields, when you need 2+ details at once. kind file: an Upload button for their log or screenshot "
            "(instead of telling them to find the file and drag it in); with what log the card also shows where the "
            "logs are per platform and launcher, and an uploaded log is analyzed before you see it. kind steps: a fix "
            "of 2-8 steps they try in order, shown one at a time with Done and Still broken buttons; you hear back "
            "which step failed, or that all are done. Labels and steps in the reply's language. Call it before "
            "writing your reply; the reply must still read fine without the controls. One set per reply: the last "
            "ask_requester or offer_action call wins."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(ai_asks.KINDS)},
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "kind choices: 2-5 short answers, up to 80 characters each.",
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                    "description": "kind select: 2-25 options; label and optional description up to 100 characters.",
                },
                "placeholder": {"type": "string", "description": "kind select: hint text, up to 100 characters."},
                "title": {"type": "string", "description": "kind form: the form's title, up to 45 characters."},
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Up to 45 characters."},
                            "type": {
                                "type": "string",
                                "enum": list(ai_asks.FIELD_TYPES),
                                "description": "short or long text, select (2-25 options), radio (2-10 options).",
                            },
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "select and radio only.",
                            },
                            "placeholder": {"type": "string", "description": "Text and select fields only."},
                            "required": {"type": "boolean", "description": "Default true."},
                        },
                        "required": ["label", "type"],
                        "additionalProperties": False,
                    },
                    "description": "kind form: 1-5 fields.",
                },
                "what": {
                    "type": "string",
                    "enum": list(ai_asks.FILE_WHATS),
                    "description": "kind file: log (latest.log or crash-report.txt), screenshot, or any. Default any.",
                },
                "label": {
                    "type": "string",
                    "description": "kind file: the button text, up to 80 characters (e.g. Upload latest.log). "
                    "Default: Upload.",
                },
                "max_files": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": ai_asks.MAX_FILES,
                    "description": f"kind file: how many files, 1-{ai_asks.MAX_FILES}. Default 1.",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"kind steps: 2-{ai_asks.MAX_STEPS} steps in order, one short action each "
                    f"(up to {ai_asks.STEP_LIMIT} characters).",
                },
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    # Not strict either: label, category, no_thanks and report_bug's prefill are optional; ui/ai_asks validates every
    # call.
    ACTION_TOOL: {
        "type": "function",
        "name": ACTION_TOOL,
        "description": (
            "Show one button under your reply that runs something for the requester when they click it (only they "
            "can). get_staff (tickets only): flags the ticket for staff and pings the helpers, like the ticket's "
            "'No, I still need help' button; offer it with a fix that may not work, never with a handoff (a handoff "
            "already calls staff). open_ticket (public channels only, never in tickets or DMs): opens the ticket form "
            "of a category, or lets them pick one; for questions that need a private ticket. link_patreon: gives "
            "them their Patreon link button, like /patreon link; when their Patreon isn't linked with the bot. "
            "report_bug (public channels and DMs, never in tickets): opens a bug report form pre-filled with title, "
            "what_happened, steps and version from the conversation, which they check and send; it posts in the "
            "bug-report forum with their recent logs and screenshots from here. For a real, unexplained DragonMineZ "
            "bug that search_known_issues didn't find. "
            "Call it before writing your reply, and say in the reply what the button does. One set per reply: the "
            "last ask_requester or offer_action call wins."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ai_asks.ACTIONS)},
                "label": {
                    "type": "string",
                    "description": f"The button text in the reply's language, up to {ai_asks.BUTTON_LABEL_LIMIT} "
                    "characters. Default: a plain name of the action.",
                },
                "category": {
                    "type": "string",
                    "enum": list(CATEGORIES),
                    "description": f"open_ticket only: {_TICKET_CATEGORIES}. Leave it out when unsure: they pick one.",
                },
                "no_thanks": {
                    "type": "boolean",
                    "description": "Also show a No thanks button that only dismisses the offer. Default false.",
                },
                "title": {
                    "type": "string",
                    "description": f"report_bug only: a short title of the bug, up to {ai_asks.BUG_LIMITS['title']} "
                    "characters.",
                },
                "what_happened": {
                    "type": "string",
                    "description": "report_bug only: what goes wrong and what they expected, from the conversation, up "
                    f"to {ai_asks.BUG_LIMITS['what_happened']} characters.",
                },
                "steps": {
                    "type": "string",
                    "description": "report_bug only: the steps to reproduce it, one per line, up to "
                    f"{ai_asks.BUG_LIMITS['steps']} characters. Leave it out when unknown.",
                },
                "version": {
                    "type": "string",
                    "description": "report_bug only: DragonMineZ, Minecraft and Forge versions when known, up to "
                    f"{ai_asks.BUG_LIMITS['version']} characters.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}
TOOL_FUNCS = {
    "get_patreon_status": get_patreon_status,
    "get_user_bug_reports": get_user_bug_reports,
    "search_known_issues": search_known_issues,
    "get_latest_releases": get_latest_releases,
    "list_mod_files": list_mod_files,
    "read_mod_file": read_mod_file,
    ASK_TOOL: ask_requester,
    ACTION_TOOL: offer_action,
}
SUPPORT_TOOL_NAMES = list(TOOL_SCHEMAS)
# Runs whose reply can't carry controls (/ask) get only the lookups.
LOOKUP_TOOL_NAMES = [name for name in SUPPORT_TOOL_NAMES if name not in CONTROL_TOOLS]


async def _labeled(label: str, coro: Any) -> str | None:
    try:
        return f"{label}: {json.dumps(await coro, ensure_ascii=False, default=str)}"
    except Exception:
        log.exception("Support prefetch failed", extra={"event": "support_prefetch_failed", "label": label})
        return None


ACCOUNT_CACHE_MAX = 500
_account_cache: dict[Any, str] = {}


async def _cached_account_line(key: Any, bot: Any, user_id: int) -> str | None:
    if key not in _account_cache:
        line = await _labeled("requester_account", get_patreon_status(str(user_id), _bot_context=bot))
        if line is None:
            return None
        if len(_account_cache) >= ACCOUNT_CACHE_MAX:
            _account_cache.pop(next(iter(_account_cache)))  # ponytail: oldest-first, no TTL; the escalation tools are live
        _account_cache[key] = line
    return _account_cache[key]


async def build_prefetch_lines(*, bot: Any, user_id: int, text: str, account_key: Any = None) -> list[str]:
    """Data for the tool-free first pass. Account data is fetched when the conversation is about it, or once
    per ticket (account_key) and reused for the rest of that conversation."""
    # ponytail: keyword routing; the escalation pass has real tools for anything this misses.
    jobs = [
        _labeled(
            "releases",
            get_latest_releases(
                include_patch_notes=bool(PATCH_NOTES_TOPIC_RE.search(text)),
                discord_user_id=str(user_id),
                _bot_context=bot,
            ),
        )
    ]
    if account_key is not None:
        jobs.append(_cached_account_line(account_key, bot, user_id))
    elif ACCOUNT_TOPIC_RE.search(text):
        jobs.append(_labeled("requester_account", get_patreon_status(str(user_id), _bot_context=bot)))
    if BUG_TOPIC_RE.search(text):
        jobs.append(_labeled("requester_bug_reports", get_user_bug_reports(str(user_id), _bot_context=bot)))
    return [line for line in await asyncio.gather(*jobs) if line]
