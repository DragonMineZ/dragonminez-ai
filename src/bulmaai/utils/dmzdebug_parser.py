"""Parser for ``/dmzdebug`` player-state dumps produced by DragonMineZ.

The mod's ``DebugCommand`` writes a single UTF-8 file (``<player>.log``) that is a
pretty-printed NBT dump with a couple of hand-written free-text sections (Party,
Quests). A newer machine-readable JSON emission is the *preferred* contract; this
module auto-detects which format a file is and parses either into a common
:class:`DebugReport`.

Reference: ``AI/dmzdebug-discord-parsing.md`` (sections 3–6). The text format erases
type information (every NBT leaf is stringified), so numeric/boolean interpretation
is deferred to the renderer via the ``as_bool`` / ``as_number`` / ``ticks_to_seconds``
helpers (a boolean and an int both look like ``1`` in the text dump).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── Detection ────────────────────────────────────────────────────────────────

# Third ``##``-wrapped line of the header carries the player name; the first is
# always the literal "DMZ DEBUG" banner.
_RE_HEADER_NAME = re.compile(r"^##\s+(.+?)\s+##$")
_HEADER_BANNER = "DMZ DEBUG"

# Section header, e.g. ``# Stats:`` or ``# PlayerQuestData (raw):``.
_RE_SECTION = re.compile(r"^# (.+):$")

# Hand-written (non-NBT) sections.
_HANDWRITTEN = {"Party", "Quests"}

# ``<uuid> (<name>)`` as produced by ``formatMember``.
_RE_MEMBER = re.compile(r"^([0-9a-fA-F-]+)\s+\((.*)\)\s*(\[LEADER])?\s*$")

# ``[<n>]:`` count header, e.g. ``Party members (client) [2]:``.
_RE_COUNT_HEADER = re.compile(r"^(.*?)\s*\[(\d+)]:\s*$")


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class DebugReport:
    """Normalised view of a dmzdebug dump, independent of source format."""

    fmt: str = "text"  # "text" | "json"
    schema_version: int | None = None
    generated_at: str | None = None

    player_name: str | None = None
    player_uuid: str | None = None
    online: bool | None = None
    dimension: str | None = None
    pos: list[float] | None = None

    mod_version: str | None = None
    mc_version: str | None = None
    forge_version: str | None = None
    dedicated: bool | None = None

    scope: str | None = None
    derived: dict[str, Any] | None = None

    # NBT sections (text: nested dict/list of raw strings; json: typed values).
    sections: dict[str, Any] = field(default_factory=dict)

    # Hand-written sections (text format only).
    party: dict[str, Any] | None = None
    quests: dict[str, Any] | None = None

    warnings: list[str] = field(default_factory=list)


# ── Public entry points ──────────────────────────────────────────────────────

def looks_like_dmzdebug(text: str) -> bool:
    """Cheap content sniff: does *text* look like a dmzdebug dump (either format)?"""
    head = text.lstrip()[:4000]
    if _HEADER_BANNER in head[:200]:
        return True
    # JSON dump: a top-level object with a schemaVersion is our contract.
    if head.startswith("{"):
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return False
        return isinstance(obj, dict) and "schemaVersion" in obj
    return False


def detect_format(text: str) -> str | None:
    """Return ``"json"``, ``"text"``, or ``None`` if it is not a dmzdebug dump."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict) and "schemaVersion" in obj:
            return "json"
    if _HEADER_BANNER in stripped[:200]:
        return "text"
    return None


def parse_debug(text: str) -> DebugReport:
    """Parse a dmzdebug dump, branching on content (not filename)."""
    fmt = detect_format(text)
    if fmt == "json":
        return _parse_json(text)
    # Default to the text parser even for unrecognised content — it degrades
    # gracefully and still surfaces whatever sections it finds.
    return _parse_text(text)


# ── JSON format (section 6) ──────────────────────────────────────────────────

# The bot supports up to this schema; newer dumps get a non-fatal warning.
SUPPORTED_SCHEMA_VERSION = 1


def _parse_json(text: str) -> DebugReport:
    obj = json.loads(text)
    report = DebugReport(fmt="json")

    report.schema_version = _as_int(obj.get("schemaVersion"))
    report.generated_at = obj.get("generatedAt")

    mod = obj.get("mod") or {}
    report.mod_version = mod.get("version")
    report.mc_version = mod.get("mc")
    report.forge_version = mod.get("forge")

    server = obj.get("server") or {}
    if "dedicated" in server:
        report.dedicated = bool(server.get("dedicated"))

    player = obj.get("player") or {}
    report.player_name = player.get("name")
    report.player_uuid = player.get("uuid")
    if "online" in player:
        report.online = bool(player.get("online"))
    report.dimension = player.get("dimension")
    pos = player.get("pos")
    if isinstance(pos, list):
        report.pos = [float(p) for p in pos if isinstance(p, (int, float))]

    report.scope = obj.get("scope")
    derived = obj.get("derived")
    if isinstance(derived, dict):
        report.derived = derived

    sections = obj.get("sections")
    if isinstance(sections, dict):
        report.sections = sections
        # PlayerQuestData carries party state in the JSON contract.
        pqd = sections.get("PlayerQuestData")
        if isinstance(pqd, dict):
            report.quests = pqd

    if report.schema_version is not None and report.schema_version > SUPPORTED_SCHEMA_VERSION:
        report.warnings.append(
            f"schemaVersion {report.schema_version} is newer than supported "
            f"({SUPPORTED_SCHEMA_VERSION}); some fields may render generically."
        )

    return report


# ── Text format (sections 3–5) ───────────────────────────────────────────────

def _parse_text(text: str) -> DebugReport:
    report = DebugReport(fmt="text")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    report.player_name = _extract_header_name(lines)

    # Split into sections keyed by their ``# Title:`` header.
    raw_sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        m = _RE_SECTION.match(line)
        if m:
            current = m.group(1)
            raw_sections[current] = []
            continue
        if current is not None:
            raw_sections[current].append(line)

    for title, body in raw_sections.items():
        if title in _HANDWRITTEN:
            if title == "Party":
                report.party = _parse_party(body)
            else:
                report.quests = _parse_quests(body)
        else:
            report.sections[title] = _parse_nbt_tree(body)

    return report


def _extract_header_name(lines: list[str]) -> str | None:
    for line in lines[:6]:
        m = _RE_HEADER_NAME.match(line.strip())
        if m and m.group(1) != _HEADER_BANNER:
            return m.group(1)
    return None


# ── NBT indent-tree parser (sections 3.2 / 3.3 / 5) ──────────────────────────

def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_nbt_tree(body: list[str]) -> Any:
    """Parse an indented NBT dump into nested dict/list of raw string leaves.

    Container vs list is decided lazily by whether a container's first child is a
    ``- `` element. Empty containers are represented as ``{}`` (harmless per spec).
    Values are kept as raw strings; type interpretation is the renderer's job.
    """
    # Keep meaningful lines but preserve trailing spaces (an empty-string leaf
    # renders as ``key: `` — stripping the space would make it look like a
    # container opener). We only drop the leading indentation for content.
    items = [ln for ln in body if ln.strip() != ""]
    pos = 0

    def parse_container(base_indent: int) -> Any:
        nonlocal pos
        first = items[pos]
        is_list = first.lstrip(" ").startswith("- ")
        container: Any = [] if is_list else {}

        while pos < len(items):
            line = items[pos]
            indent = _indent_of(line)
            if indent < base_indent:
                break
            content = line[indent:]  # strip leading indent only

            if indent > base_indent:
                # Orphan deeper line without a recognised opener — skip.
                pos += 1
                continue

            if content == "(empty)":
                pos += 1
                continue

            if content.startswith("- "):
                elem = content[2:]
                if elem.endswith(":"):
                    # List element that is a compound: ``- <i>:`` then fields deeper.
                    pos += 1
                    child = _consume_child(base_indent)
                    if isinstance(container, list):
                        container.append(child)
                else:
                    if isinstance(container, list):
                        container.append(elem)
                    pos += 1
                continue

            if content.endswith(":"):
                # Container opener: ``key:`` with nothing after the colon.
                key = content[:-1]
                pos += 1
                if isinstance(container, dict):
                    container[key] = _consume_child(base_indent)
                continue

            # Leaf: ``key: value`` (split on the first ": " only).
            if ": " in content:
                key, val = content.split(": ", 1)
            elif content.endswith(": "):
                key, val = content[:-2], ""
            else:
                # Malformed — bucket the whole thing under itself.
                key, val = content, ""
            if isinstance(container, dict):
                container[key] = val
            pos += 1

        return container

    def _consume_child(parent_indent: int) -> Any:
        """Parse the child container of an opener, using the real child indent."""
        nonlocal pos
        if pos < len(items) and _indent_of(items[pos]) > parent_indent:
            return parse_container(_indent_of(items[pos]))
        return {}

    if not items:
        return {}
    return parse_container(_indent_of(items[0]))


# ── Hand-written Party parser (section 3.4) ──────────────────────────────────

def _parse_party(body: list[str]) -> dict[str, Any]:
    party: dict[str, Any] = {
        "client": {"members": []},
        "server": {"members": []},
        "pending_invite": None,
    }
    lines = [ln.rstrip() for ln in body]
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("In party (client view):"):
            party["client"]["in_party"] = _val_bool(stripped)
        elif stripped.startswith("Active party id (client):"):
            party["client"]["party_id"] = _val_after(stripped)
        elif stripped.startswith("Party leader (client):"):
            party["client"]["leader"] = _parse_member_tail(_val_after(stripped))
        elif stripped.startswith("Party members (client)"):
            members, i = _consume_member_list(lines, i)
            party["client"]["members"] = members
            continue
        elif stripped.startswith("Pending invite:"):
            party["pending_invite"] = _parse_pending_invite(_val_after(stripped))
        elif stripped.startswith("Server party (PartySavedData):"):
            party["server"]["in_party"] = "not in a server party" not in stripped
        elif stripped.startswith("Server party id:"):
            party["server"]["party_id"] = _val_after(stripped)
            party["server"]["in_party"] = True
        elif stripped.startswith("Server leader:"):
            party["server"]["leader"] = _parse_member_tail(_val_after(stripped))
        elif stripped.startswith("Server PvP enabled:"):
            party["server"]["pvp_enabled"] = _val_bool(stripped)
        elif stripped.startswith("Server members"):
            members, i = _consume_member_list(lines, i)
            party["server"]["members"] = members
            continue
        i += 1

    return party


def _consume_member_list(lines: list[str], header_idx: int) -> tuple[list[dict[str, Any]], int]:
    """Read a ``... [<n>]:`` header and its indented ``- <uuid> (<name>)`` rows."""
    members: list[dict[str, Any]] = []
    i = header_idx + 1
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped == "(none)":
            i += 1
            break
        if stripped.startswith("- "):
            member = _parse_member_tail(stripped[2:])
            if member:
                members.append(member)
            i += 1
            continue
        break  # next real section line
    return members, i


def _parse_member_tail(value: str | None) -> dict[str, Any] | None:
    if not value or value == "null":
        return None
    m = _RE_MEMBER.match(value.strip())
    if not m:
        return {"raw": value.strip()}
    return {
        "uuid": m.group(1),
        "name": m.group(2),
        "leader": bool(m.group(3)),
    }


def _parse_pending_invite(value: str | None) -> dict[str, Any] | None:
    if not value or value == "none":
        return None
    expired = "[EXPIRED]" in value
    cleaned = value.replace("[EXPIRED]", "").strip()
    # ``from <name> (<uuid>)``
    m = re.match(r"^from\s+(.*?)\s+\(([0-9a-fA-F-]+)\)$", cleaned)
    if m:
        return {"name": m.group(1), "uuid": m.group(2), "expired": expired}
    return {"raw": cleaned, "expired": expired}


# ── Hand-written Quests parser (section 3.4) ─────────────────────────────────

def _parse_quests(body: list[str]) -> dict[str, Any]:
    quests: dict[str, Any] = {
        "tracked": None,
        "last_completed": None,
        "completed": [],
        "in_progress": [],
        "failed": [],
    }
    lines = [ln.rstrip() for ln in body]
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("Tracked quest:"):
            quests["tracked"] = _quest_or_none(_val_after(stripped))
        elif stripped.startswith("Last completed quest:"):
            quests["last_completed"] = _parse_last_completed(_val_after(stripped))
        elif stripped.startswith("Completed"):
            items, i = _consume_quest_list(lines, i)
            quests["completed"] = items
            continue
        elif stripped.startswith("In progress"):
            items, i = _consume_quest_list(lines, i)
            quests["in_progress"] = items
            continue
        elif stripped.startswith("Failed"):
            items, i = _consume_quest_list(lines, i)
            quests["failed"] = items
            continue
        i += 1

    return quests


def _consume_quest_list(lines: list[str], header_idx: int) -> tuple[list[str], int]:
    items: list[str] = []
    i = header_idx + 1
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped == "(none)":
            i += 1
            break
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
            i += 1
            continue
        break
    return items, i


def _parse_last_completed(value: str | None) -> dict[str, Any] | None:
    quest = _quest_or_none(value)
    if quest is None:
        return None
    # ``<questId> (status <STATUS>)``
    m = re.match(r"^(\S+)\s*\(status\s+(\w+)\)$", quest)
    if m:
        return {"quest_id": m.group(1), "status": m.group(2)}
    return {"quest_id": quest, "status": None}


def _quest_or_none(value: str | None) -> str | None:
    if not value or value == "none":
        return None
    return value


# ── Small value helpers ──────────────────────────────────────────────────────

def _val_after(line: str) -> str | None:
    """Return the text after the last ``: `` on a hand-written line."""
    if ": " not in line:
        return None
    return line.split(": ", 1)[1].strip() or None


def _val_bool(line: str) -> bool | None:
    raw = _val_after(line)
    if raw is None:
        return None
    return raw.strip().lower() in ("true", "1", "yes")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── Typed accessors for renderers (bridge text/json) ─────────────────────────

def as_bool(value: Any) -> bool | None:
    """Interpret a leaf (raw text ``0``/``1`` or a real JSON bool) as a bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes"):
            return True
        if v in ("0", "false", "no", ""):
            return False
    return None


def as_number(value: Any) -> float | None:
    """Interpret a leaf as a number, tolerating text like ``250.0`` or ints."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def ticks_to_seconds(ticks: Any) -> float | None:
    """Convert a tick count (20 ticks = 1s) to seconds, rounded to 1 decimal."""
    n = as_number(ticks)
    if n is None:
        return None
    return round(n / 20.0, 1)
