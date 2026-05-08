import asyncio
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FAQ_SUGGESTION_INSTRUCTIONS = """You generate DragonMineZ support FAQ candidates.

Use only the provided support trace sources. Propose a candidate only when the
same user-facing question is useful for future support and the answer is safe to
reuse. Do not invent policies, bypasses, staff-only actions, account decisions,
private user data, or unsupported claims.

Return JSON with this shape:
{"candidates":[{"question":"...","answer":"...","language":"en","tags":["..."],"source_trace_ids":[1],"confidence":0.8,"rationale":"..."}]}
"""

FAQ_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "language": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "source_trace_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "question",
                    "answer",
                    "language",
                    "tags",
                    "source_trace_ids",
                    "confidence",
                    "rationale",
                ],
            },
        }
    },
    "required": ["candidates"],
}

SPEAKER_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SupportFAQSource:
    trace_id: int
    created_at: str
    language: str
    channel_id: str | None
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class FAQCandidate:
    question: str
    answer: str
    language: str = "en"
    tags: tuple[str, ...] = ()
    source_trace_ids: tuple[int, ...] = ()
    confidence: float = 0.0
    rationale: str = ""


def _clean_text(value: Any) -> str:
    text = str(value or "").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _message_content(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return _clean_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return _clean_text("\n".join(parts))
    return ""


def _strip_speaker_prefix(content: str) -> str:
    stripped = SPEAKER_PREFIX_RE.sub("", content, count=1).strip()
    return stripped or content.strip()


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def support_trace_to_faq_source(row: Any) -> SupportFAQSource:
    input_json = _row_value(row, "input_json", []) or []
    question = ""
    for message in reversed(input_json):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        question = _strip_speaker_prefix(_message_content(message))
        if question:
            break

    return SupportFAQSource(
        trace_id=int(_row_value(row, "id")),
        created_at=str(_row_value(row, "created_at")),
        language=str(_row_value(row, "language") or "en"),
        channel_id=(
            str(_row_value(row, "channel_id"))
            if _row_value(row, "channel_id") is not None
            else None
        ),
        question=question,
        answer=_clean_text(_row_value(row, "reply_text")),
    )


def _normalize_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = re.sub(r"\s+", "-", str(item or "").strip().lower())
        tag = re.sub(r"[^a-z0-9_-]", "", tag)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tuple(tags[:8])


def _normalize_trace_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    ids: list[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            trace_id = int(item)
        except (TypeError, ValueError):
            continue
        if trace_id <= 0 or trace_id in seen:
            continue
        seen.add(trace_id)
        ids.append(trace_id)
    return tuple(ids)


def normalize_faq_candidates(
    payload: Any,
    *,
    min_confidence: float = 0.6,
) -> list[FAQCandidate]:
    raw_candidates = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(raw_candidates, list):
        return []

    candidates: list[FAQCandidate] = []
    seen_questions: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        question = _clean_text(raw.get("question"))
        answer = _clean_text(raw.get("answer"))
        if not question or not answer:
            continue
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < min_confidence:
            continue

        question_key = re.sub(r"\W+", " ", question).strip().casefold()
        if not question_key or question_key in seen_questions:
            continue
        seen_questions.add(question_key)

        language = str(raw.get("language") or "en").strip().lower()[:8] or "en"
        candidates.append(
            FAQCandidate(
                question=question,
                answer=answer,
                language=language,
                tags=_normalize_tags(raw.get("tags")),
                source_trace_ids=_normalize_trace_ids(raw.get("source_trace_ids")),
                confidence=max(0.0, min(confidence, 1.0)),
                rationale=_clean_text(raw.get("rationale")),
            )
        )
    return candidates


def _candidate_sort_key(candidate: FAQCandidate) -> tuple[str, str]:
    return (candidate.language, candidate.question.casefold())


def render_faq_markdown(
    candidates: Iterable[FAQCandidate],
    *,
    title: str = "DragonMineZ Generated FAQ",
) -> str:
    lines = [
        f"# {title}",
        "",
        "These entries are generated from approved support traces for OpenAI file_search.",
        "Review and edit answers before uploading to the production vector store.",
        "",
    ]
    for candidate in sorted(candidates, key=_candidate_sort_key):
        lines.extend(
            [
                f"## {candidate.question}",
                "",
                candidate.answer,
                "",
                f"Language: {candidate.language}",
            ]
        )
        if candidate.tags:
            lines.append(f"Tags: {', '.join(candidate.tags)}")
        if candidate.source_trace_ids:
            lines.append(
                "Source traces: "
                + ", ".join(str(trace_id) for trace_id in candidate.source_trace_ids)
            )
        if candidate.confidence:
            lines.append(f"Suggestion confidence: {candidate.confidence:.2f}")
        if candidate.rationale:
            lines.append(f"Rationale: {candidate.rationale}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_faq_markdown(candidates: Iterable[FAQCandidate], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_faq_markdown(candidates), encoding="utf-8", newline="\n")


def _extract_response_json(response: Any) -> Any:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return json.loads(output_text)

    text_parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) == "output_text":
                text_parts.append(getattr(part, "text", ""))
    return json.loads("".join(text_parts))


def _sources_payload(sources: Sequence[SupportFAQSource]) -> list[dict[str, Any]]:
    return [
        {
            "trace_id": source.trace_id,
            "created_at": source.created_at,
            "language": source.language,
            "channel_id": source.channel_id,
            "question": source.question,
            "answer": source.answer,
        }
        for source in sources
        if source.question and source.answer
    ]


async def suggest_faq_candidates(
    sources: Sequence[SupportFAQSource],
    *,
    openai_client: Any | None = None,
    model: str = "gpt-5.4-mini",
    max_candidates: int = 20,
    min_confidence: float = 0.6,
    timeout_seconds: int = 60,
) -> list[FAQCandidate]:
    source_payload = _sources_payload(sources)
    if not source_payload:
        return []
    resolved_client = openai_client
    if resolved_client is None:
        from bulmaai.services.openai_client import client as resolved_client

    response = await asyncio.wait_for(
        resolved_client.responses.create(
            model=model,
            instructions=FAQ_SUGGESTION_INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "Suggest reusable FAQ candidates from these support traces.",
                            "max_candidates": max(1, min(int(max_candidates), 20)),
                            "sources": source_payload,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            max_output_tokens=3000,
            metadata={
                "app": "dragonminez-ai",
                "workflow": "support_faq_suggestion",
                "source_count": str(len(source_payload)),
            },
            store=True,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "support_faq_candidates",
                    "schema": FAQ_CANDIDATE_SCHEMA,
                    "strict": True,
                }
            },
        ),
        timeout=timeout_seconds,
    )
    return normalize_faq_candidates(_extract_response_json(response), min_confidence=min_confidence)


async def publish_faq_markdown_to_vector_store(
    faq_path: Path,
    *,
    vector_store_id: str,
    openai_client: Any | None = None,
) -> dict[str, str]:
    resolved_client = openai_client
    if resolved_client is None:
        from bulmaai.services.openai_client import client as resolved_client

    with faq_path.open("rb") as handle:
        uploaded_file = await resolved_client.files.create(file=handle, purpose="assistants")

    uploaded_file_id = str(getattr(uploaded_file, "id"))
    vector_store_file = await resolved_client.vector_stores.files.create(
        vector_store_id=vector_store_id,
        file_id=uploaded_file_id,
    )
    return {
        "file_id": uploaded_file_id,
        "vector_store_file_id": str(getattr(vector_store_file, "id")),
    }
