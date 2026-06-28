import json
import logging
from dataclasses import dataclass, field

from openai import AsyncOpenAI

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
behaviour. Keep every field short.
""".strip()

TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_bug": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "severity": {"type": "string", "enum": list(VALID_SEVERITIES)},
        "affected_area": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_bug", "title", "summary", "severity", "affected_area", "steps"],
}


@dataclass(slots=True)
class BugTriage:
    is_bug: bool
    title: str
    summary: str
    severity: str
    affected_area: str
    steps: list[str] = field(default_factory=list)


def _coerce_triage(data: dict, *, fallback_title: str) -> BugTriage:
    severity = str(data.get("severity", "medium")).strip().lower()
    if severity not in VALID_SEVERITIES:
        severity = "medium"
    raw_steps = data.get("steps") or []
    steps = [str(step).strip() for step in raw_steps if str(step).strip()][:10]
    title = str(data.get("title") or fallback_title).strip()[:240] or fallback_title
    return BugTriage(
        is_bug=bool(data.get("is_bug", False)),
        title=title,
        summary=str(data.get("summary") or "").strip()[:1500],
        severity=severity,
        affected_area=str(data.get("affected_area") or "Unknown").strip()[:100],
        steps=steps,
    )


async def analyze_bug_report(
    client: AsyncOpenAI,
    *,
    model: str,
    report_text: str,
    attachments: list[str] | None = None,
    fallback_title: str = "Bug report",
) -> BugTriage:
    """Run a lightweight model to turn a raw forum post into structured triage."""
    parts = [report_text.strip() or "(no text provided)"]
    if attachments:
        parts.append("Attachments: " + ", ".join(attachments[:10]))
    user_input = "\n\n".join(parts)[:6000]

    request_kwargs: dict = {
        "model": model,
        "instructions": TRIAGE_INSTRUCTIONS,
        "input": user_input,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "bug_triage",
                "schema": TRIAGE_SCHEMA,
                "strict": True,
            }
        },
    }
    if model.startswith("gpt-5"):
        request_kwargs["reasoning"] = {"effort": "low"}

    response = await client.responses.create(**request_kwargs)
    raw = (response.output_text or "").strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Bug triage returned non-JSON output: %r", raw[:200])
        data = {}
    if not isinstance(data, dict):
        data = {}
    return _coerce_triage(data, fallback_title=fallback_title)
