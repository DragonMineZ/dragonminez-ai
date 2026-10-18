import json
import logging
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic

from bulmaai.services import ai_budget
from bulmaai.services.ai_guard import defuse_mentions, strip_links
from bulmaai.services.ai_oneshot import json_output_config, response_text
from bulmaai.utils.log_parser import LogReport

log = logging.getLogger(__name__)

VALID_SEVERITIES = ("low", "medium", "high", "critical")

TRIAGE_INSTRUCTIONS = """
You are a bug-report triage assistant for a Minecraft Dragon Ball Z mod called DragonMineZ.
A player has posted a report in the community bug-report forum. The text may be in any
language and may be vague, off-topic, or not actually describe a bug.

Analyse the report and return a concise, structured triage in English that a developer can
act on. Rewrite a clear issue title, summarise the problem, list any reproduction steps the
player gave, and judge whether this is plausibly a real bug.

Be conservative with `is_bug`: questions, feature requests, suggestions, or empty/unclear
posts are NOT bugs. Only mark `is_bug` true when the report describes broken or unexpected
behaviour.

Set `game_breaking` true only when the player can't keep playing: the game or server crashes
(on load or in play), a world or save is corrupted, or they can't join or load a world at all.
Visual glitches, balance issues and minor bugs are not game-breaking.

A "LOG DIGEST" section, when present, is parsed from the player's attached log files: use its
versions and errors in the summary and affected area. Never invent errors, versions or steps
that neither the report nor the digest contains. Keep every field short.
""".strip()

TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_bug": {"type": "boolean"},
        "game_breaking": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "severity": {"type": "string", "enum": list(VALID_SEVERITIES)},
        "affected_area": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_bug", "game_breaking", "title", "summary", "severity", "affected_area", "steps"],
}


@dataclass(slots=True)
class BugTriage:
    is_bug: bool
    title: str
    summary: str
    severity: str
    affected_area: str
    steps: list[str] = field(default_factory=list)
    game_breaking: bool = False


DUPLICATE_INSTRUCTIONS = """
You compare a new DragonMineZ bug report against a list of existing GitHub issues from the
same project. Decide whether the new report is one of:

- "duplicate": it describes the same underlying problem as an issue that is still OPEN.
- "already_fixed": it describes the same underlying problem as an issue that is CLOSED
  (and not closed as "not planned"), i.e. the fix already exists or is on the way.
- "none": no listed issue clearly matches, or you are not confident.

Be conservative. Only claim a match when the issues clearly describe the same behaviour, not
merely the same feature area. When unsure, return "none". Return the matching issue number
and a one-sentence reason.
""".strip()

DUPLICATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "match_type": {"type": "string", "enum": ["none", "duplicate", "already_fixed"]},
        "issue_number": {"type": ["integer", "null"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["match_type", "issue_number", "confidence", "reason"],
}


@dataclass(slots=True)
class DuplicateAssessment:
    match_type: str  # "none" | "duplicate" | "already_fixed"
    issue_number: int | None
    issue_title: str
    confidence: str
    reason: str

    @property
    def has_match(self) -> bool:
        return self.match_type in ("duplicate", "already_fixed") and self.issue_number is not None


_NO_MATCH = DuplicateAssessment("none", None, "", "low", "")


def _clean(value: object) -> str:
    """Model output built from member text (and GitHub titles anyone can write) is posted under the bot's
    name in a public thread and can become a GitHub issue: no pings, no links."""
    return strip_links(defuse_mentions(str(value or ""))).strip()


def _coerce_triage(data: dict, *, fallback_title: str) -> BugTriage:
    severity = str(data.get("severity", "medium")).strip().lower()
    if severity not in VALID_SEVERITIES:
        severity = "medium"
    raw_steps = data.get("steps") or []
    steps = [_clean(step) for step in raw_steps if _clean(step)][:10]
    title = _clean(data.get("title") or fallback_title)[:240] or _clean(fallback_title) or "Bug report"
    return BugTriage(
        is_bug=bool(data.get("is_bug", False)),
        title=title,
        summary=_clean(data.get("summary"))[:1500],
        severity=severity,
        affected_area=_clean(data.get("affected_area") or "Unknown")[:100],
        steps=steps,
        game_breaking=bool(data.get("game_breaking", False)),
    )


def log_digest(filename: str, report: LogReport) -> str:
    """The parts of a parsed log that say what broke, for the triage prompt."""
    known = (
        ("Minecraft", report.mc_version),
        ("Forge", report.forge_version),
        ("DragonMineZ", report.dragonminez_version),
        ("Java", report.java_version),
    )
    versions = ", ".join(f"{name} {value}" for name, value in known if value) or "versions not found"
    lines = [f"{filename}: {versions}, {len(report.mods)} mods"]
    if report.errors:
        lines += ["Errors:", *report.errors[:8]]
    if report.stacktrace:
        lines += ["Stack trace:", report.stacktrace]
    return "\n".join(lines)


async def analyze_bug_report(
    client: AsyncAnthropic,
    *,
    model: str,
    report_text: str,
    attachments: list[str] | None = None,
    fallback_title: str = "Bug report",
    log_digest: str = "",
) -> BugTriage:
    """Run a lightweight model to turn a raw forum post into structured triage."""
    parts = [report_text.strip()[:5000] or "(no text provided)"]
    if attachments:
        parts.append("Attachments: " + ", ".join(attachments[:10]))
    if log_digest:
        parts.append("LOG DIGEST:\n" + log_digest[:4000])
    user_input = "\n\n".join(parts)

    request_kwargs: dict = {
        "model": model,
        "system": TRIAGE_INSTRUCTIONS,
        "messages": [{"role": "user", "content": user_input}],
        "max_tokens": 6000,
        "output_config": json_output_config(TRIAGE_SCHEMA),
    }

    response = await client.messages.create(**request_kwargs)
    ai_budget.record_response(response)
    raw = response_text(response, "bug_triage").strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Bug triage returned non-JSON output: %r", raw[:200])
        data = {}
    if not isinstance(data, dict):
        data = {}
    return _coerce_triage(data, fallback_title=fallback_title)


async def assess_duplicate(
    client: AsyncAnthropic,
    *,
    model: str,
    report_text: str,
    candidates: list[dict],
) -> DuplicateAssessment:
    """Ask the model whether the report duplicates, or is already fixed by, an existing issue.

    `candidates` are GitHub issue dicts (number, title, state, state_reason). Returns a
    conservative assessment; on any error or empty candidate list, returns a "none" match.
    """
    usable = [c for c in candidates if c.get("number") and c.get("title")]
    if not usable:
        return _NO_MATCH

    titles_by_number = {int(c["number"]): str(c["title"]) for c in usable}
    lines = []
    for c in usable[:15]:
        state = str(c.get("state") or "open")
        reason = str(c.get("state_reason") or "")
        state_label = f"{state}/{reason}" if reason else state
        lines.append(f"#{int(c['number'])} [{state_label}] {c['title']}")
    candidate_block = "\n".join(lines)

    user_input = (
        f"NEW REPORT:\n{report_text.strip()[:3000] or '(no text provided)'}\n\n"
        f"EXISTING ISSUES:\n{candidate_block[:3000]}"
    )

    request_kwargs: dict = {
        "model": model,
        "system": DUPLICATE_INSTRUCTIONS,
        "messages": [{"role": "user", "content": user_input}],
        "max_tokens": 4000,
        "output_config": json_output_config(DUPLICATE_SCHEMA),
    }

    try:
        response = await client.messages.create(**request_kwargs)
        ai_budget.record_response(response)
        data = json.loads(response_text(response, "bug_duplicate").strip())
    except (json.JSONDecodeError, TypeError):
        log.warning("Duplicate assessment returned non-JSON output")
        return _NO_MATCH
    except Exception:
        log.exception("Duplicate assessment request failed")
        return _NO_MATCH
    if not isinstance(data, dict):
        return _NO_MATCH

    match_type = str(data.get("match_type") or "none")
    if match_type not in ("duplicate", "already_fixed"):
        return _NO_MATCH
    raw_number = data.get("issue_number")
    issue_number = int(raw_number) if isinstance(raw_number, int) else None
    if issue_number is None or issue_number not in titles_by_number:
        return _NO_MATCH

    confidence = str(data.get("confidence") or "low").strip().lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "low"
    return DuplicateAssessment(
        match_type=match_type,
        issue_number=issue_number,
        issue_title=_clean(titles_by_number[issue_number]),
        confidence=confidence,
        reason=_clean(data.get("reason"))[:300],
    )
