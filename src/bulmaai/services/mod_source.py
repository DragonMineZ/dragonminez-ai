"""DragonMineZ's own files, read-only, for the support AI's list_mod_files / read_mod_file tools (services/ai_tools;
lookups, so /ask and staff drafts get them too).

Only the allowlist in AREAS is ever listed or read: the config classes, the config and quest files the mod ships, the
server commands with DMZServer and DMZPermissions, and the English lang file. They come from GITHUB_DEFAULT_REPO on
settings.mod_source_branch through the GitHub App: one Git tree call for every listing, the Contents API per file.
Errors come back as {"error": ...} for the model, never as exceptions.

ponytail: in-memory cache of the tree and of file texts per (branch, path) for CACHE_SECONDS, without a size cap: the
allowlist keeps it to a few MB. A restart only refetches."""

import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from requests import HTTPError

from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService

log = logging.getLogger(__name__)

JAVA_DIR = "src/main/java/com/dragonminez/"
DATA_DIR = "src/main/resources/data/dragonminez/"
DEFAULTS_DIR = DATA_DIR + "previousConfigs/"
QUESTS_DIR = DATA_DIR + "previousQuests/"
LANG_FILE = "src/main/resources/assets/dragonminez/lang/en_us.json"
# area -> path patterns: "dir/*" the files in dir, "dir/**" every file under it, anything else one exact file.
AREAS: dict[str, tuple[str, ...]] = {
    "config": (JAVA_DIR + "common/config/*",),
    "defaults": (DEFAULTS_DIR + "**",),
    "commands": (JAVA_DIR + "server/commands/*", JAVA_DIR + "server/DMZServer.java"),
    "lang": (LANG_FILE,),
    "quests": (QUESTS_DIR + "**",),
}
SUFFIXES = (".java", ".json")
# Where a game writes its configs; the same layout as DEFAULTS_DIR, so a player's path reads the shipped file.
RUNTIME_CONFIG_DIR = "config/dragonminez/"
MAX_LISTED = 80
MAX_CHARS = 6000
MAX_LINE_CHARS = 300
CONTEXT_LINES = 3
QUERY_MAX = 100
CACHE_SECONDS = 600.0
GITHUB_FAILED = "Couldn't read the mod's source from GitHub right now. Answer without it."
NOT_ALLOWED = (
    "That path can't be read. Use a path from list_mod_files: the config classes, the shipped config and quest "
    "files, the commands or the lang file."
)
_SAFE_PATH_RE = re.compile(r"[A-Za-z0-9_.\-/]+")

_cache: dict[tuple[str, str], tuple[float, Any]] = {}


def clear_cache() -> None:
    _cache.clear()


async def _cached(key: tuple[str, str], fetch: Callable[[], Awaitable[Any]]) -> Any:
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = await fetch()
    _cache[key] = (time.monotonic(), value)
    return value


def _matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2]) and len(path) > len(pattern) - 2
    if pattern.endswith("/*"):
        directory = pattern[:-1]
        return path.startswith(directory) and "/" not in path[len(directory) :] and len(path) > len(directory)
    return path == pattern


def area_of(path: str) -> str | None:
    """The area an allowlisted repo path belongs to; None for anything else."""
    if not path.endswith(SUFFIXES):
        return None
    return next((area for area, patterns in AREAS.items() if any(_matches(item, path) for item in patterns)), None)


def normalize(path: Any) -> str | None:
    """The repo path for what the model asked, or None when it may not be read: only plain relative paths (no
    absolute paths, drive letters, '.', '..', empty parts, '%' or other odd characters) inside AREAS."""
    path = str(path or "").strip().replace("\\", "/")
    if path.startswith(RUNTIME_CONFIG_DIR):
        path = DEFAULTS_DIR + path[len(RUNTIME_CONFIG_DIR) :]
    if not _SAFE_PATH_RE.fullmatch(path) or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path if area_of(path) else None


def _service(settings: Any) -> GitHubService:
    return GitHubService(
        auth=GitHubAppAuth(
            app_id=settings.GH_APP_ID,
            installation_id=settings.GH_INSTALLATION_ID,
            private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM,
        ),
        owner=settings.GITHUB_OWNER,
        repo=settings.GITHUB_DEFAULT_REPO,
        base_branch=settings.GITHUB_BASE_BRANCH,
    )


async def _tree(settings: Any, branch: str) -> list[str]:
    return await _cached((branch, ""), lambda: _service(settings).list_tree(branch))


async def _text(settings: Any, branch: str, path: str) -> str:
    async def fetch() -> str:
        text, _sha = await _service(settings).get_file(path, ref=branch)
        return text

    return await _cached((branch, path), fetch)


async def list_files(settings: Any, area: str, folder: str | None = None) -> dict[str, Any]:
    """The allowlisted files of one area, sorted, at most MAX_LISTED. folder narrows it to one folder of the area;
    a cut listing also shows every folder with its file count."""
    if area not in AREAS:
        return {"error": f"Unknown area {area!r}. Use one of: {', '.join(AREAS)}."}
    branch = settings.mod_source_branch
    folder = str(folder or "").strip().replace("\\", "/").strip("/")
    if folder and not _SAFE_PATH_RE.fullmatch(folder):
        return {"error": "Unknown folder. Use one from the folders list."}
    try:
        tree = await _tree(settings, branch)
    except Exception:
        log.exception("Mod source listing failed", extra={"branch": branch})
        return {"error": GITHUB_FAILED}
    files = sorted(path for path in tree if area_of(path) == area and (not folder or path.startswith(folder + "/")))
    if folder and not files:
        return {"error": f"No {area} files under {folder}. Use a folder from the folders list."}
    result: dict[str, Any] = {
        "area": area,
        "branch": branch,
        "files": files[:MAX_LISTED],
        "total": len(files),
        "truncated": len(files) > MAX_LISTED,
    }
    if result["truncated"]:
        result["folders"] = dict(sorted(Counter(path.rsplit("/", 1)[0] for path in files).items()))
    return result


def _numbered(lines: list[str], index: int) -> str:
    line = lines[index].rstrip()
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + "…"
    return f"{index + 1}: {line}"


def _window(lines: list[str], start: int) -> tuple[str, bool]:
    """Lines from start (1-based) until MAX_CHARS; True when the file goes on."""
    out: list[str] = []
    used = 0
    for index in range(start - 1, len(lines)):
        line = _numbered(lines, index)
        if out and used + len(line) + 1 > MAX_CHARS:
            return "\n".join(out), True
        out.append(line)
        used += len(line) + 1
    return "\n".join(out), False


def _around(lines: list[str], query: str) -> tuple[str, bool, int]:
    """The lines containing query (case-insensitive) with CONTEXT_LINES around each, overlapping stretches merged
    and separated by '...', until MAX_CHARS. (text, cut short, number of matching lines)."""
    needle = query.casefold()
    hits = [index for index, line in enumerate(lines) if needle in line.casefold()]
    stretches: list[list[int]] = []
    for index in hits:
        low, high = max(index - CONTEXT_LINES, 0), min(index + CONTEXT_LINES, len(lines) - 1)
        if stretches and low <= stretches[-1][1] + 1:
            stretches[-1][1] = high
        else:
            stretches.append([low, high])
    out: list[str] = []
    used = 0
    for low, high in stretches:
        for index in range(low, high + 1):
            line = _numbered(lines, index)
            if index == low and out:
                line = f"...\n{line}"
            if out and used + len(line) + 1 > MAX_CHARS:
                return "\n".join(out), True, len(hits)
            out.append(line)
            used += len(line) + 1
    return "\n".join(out), False, len(hits)


async def read_file(
    settings: Any, path: str, query: str | None = None, start_line: int | None = None
) -> dict[str, Any]:
    """One allowlisted file as numbered lines: around query's matches, or a window from start_line (1-based)."""
    repo_path = normalize(path)
    if repo_path is None:
        return {"error": NOT_ALLOWED}
    query = str(query or "").strip()
    if len(query) > QUERY_MAX:
        return {"error": f"query is too long (at most {QUERY_MAX} characters)."}
    branch = settings.mod_source_branch
    try:
        text = await _text(settings, branch, repo_path)
    except HTTPError as error:
        if getattr(error.response, "status_code", None) == 404:
            return {"error": f"{repo_path} doesn't exist on {branch}. Use a path from list_mod_files."}
        log.exception("Mod source read failed", extra={"branch": branch, "path": repo_path})
        return {"error": GITHUB_FAILED}
    except Exception:
        log.exception("Mod source read failed", extra={"branch": branch, "path": repo_path})
        return {"error": GITHUB_FAILED}
    lines = text.splitlines()
    result: dict[str, Any] = {"path": repo_path, "branch": branch}
    if query:
        shown, truncated, matches = _around(lines, query)
        return {**result, "query": query, "matches": matches, "lines": shown, "total_lines": len(lines), "truncated": truncated}
    try:
        start = max(int(start_line or 1), 1)
    except (TypeError, ValueError):
        start = 1
    if lines and start > len(lines):
        return {"error": f"start_line is past the end of {repo_path} ({len(lines)} lines)."}
    shown, truncated = _window(lines, start)
    return {**result, "lines": shown, "total_lines": len(lines), "truncated": truncated}
