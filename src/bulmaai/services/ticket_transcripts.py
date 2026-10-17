"""Close-out pipeline for Discord support tickets.

Turns a closed ticket's messages into a staff transcript, an AI summary, an
anonymized knowledge markdown file for OpenAI file_search, and a DB row.
Also caches screenshot analyses so images are never re-analyzed.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bulmaai.database.db import get_pool
from bulmaai.services import ai_budget, support_info
from bulmaai.services.ai_guard import defuse_mentions, strip_links
from bulmaai.services.support_faq import _extract_response_json

SPEAKER_LABELS = {
    "requester": "Requester",
    "staff": "Staff",
    "assistant": "BulmaAI",
    "participant": "User",
    "system": "System",  # notices we add, e.g. how many messages were sent while the ticket was closed
}

_MIN_ANONYMIZED_NAME_LENGTH = 3
_TRANSCRIPT_TAIL_CHARS = 30000

TICKET_SUMMARY_INSTRUCTIONS = """You summarize closed DragonMineZ (Minecraft mod) Discord support tickets.

Use only the provided transcript; never invent details not present in it. Produce a JSON summary with:
- title: a short descriptive title, at most 80 characters.
- problem: 1-3 sentences describing the requester's problem.
- resolution: 1-3 sentences describing how it was resolved, or "No resolution reached." if none.
- resolved: true only if the requester confirmed the fix or the conversation clearly shows it fixed.
- tags: up to 5 short lowercase tags relevant to the problem.
- knowledge_worthy: true only if there is a concrete, reusable DragonMineZ support problem AND a
  confirmed working resolution useful for future users. False for spam, greetings, whitelist-only
  requests, account/payment specifics, or chats that end unresolved.
- support_info: null unless the ticket reveals something future support should know. Then an object with:
  - kind: "temporary" for a current problem with a workaround (e.g. a broken release, an outage), or
    "permanent" for a reusable answer to a question likely to come up again (a common how-to, a tool or
    file users keep asking for, a frequent misconfiguration).
  - title: at most 80 characters.
  - body: at most 600 characters, written as guidance for a support agent (what to tell users, what to check).
    Copy the specifics staff gave verbatim: download links, file names, commands, version numbers, settings.
    An entry that says a tool or file exists without saying where to get it is useless.
  Null for one-off problems or account/payment specifics.

Never include personal data (usernames, ids, real names) anywhere in the summary.
"""

TICKET_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "problem": {"type": "string"},
        "resolution": {"type": "string"},
        "resolved": {"type": "boolean"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "knowledge_worthy": {"type": "boolean"},
        "support_info": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": list(support_info.KINDS)},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["kind", "title", "body"],
                },
                {"type": "null"},
            ]
        },
    },
    "required": ["title", "problem", "resolution", "resolved", "tags", "knowledge_worthy", "support_info"],
}


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    created_at: datetime
    speaker_kind: str
    speaker_name: str
    content: str


@dataclass(frozen=True, slots=True)
class SupportInfoSuggestion:
    """Support info the summary model proposes; staff approve it before the AI uses it (cogs/support_info.py)."""

    kind: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class TicketSummary:
    title: str
    problem: str
    resolution: str
    resolved: bool
    tags: tuple[str, ...]
    knowledge_worthy: bool
    support_info: SupportInfoSuggestion | None = None


async def get_image_analyses(
    attachment_ids: Sequence[int],
    *,
    pool: Any | None = None,
) -> dict[int, str]:
    ids = [int(attachment_id) for attachment_id in attachment_ids]
    if not ids:
        return {}

    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT attachment_id, analysis
            FROM ticket_image_analyses
            WHERE attachment_id = ANY($1::bigint[])
            """,
            ids,
        )
    return {int(row["attachment_id"]): str(row["analysis"]) for row in rows}


async def save_image_analysis(
    *,
    attachment_id: int,
    channel_id: int,
    analysis: str,
    pool: Any | None = None,
) -> None:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ticket_image_analyses (attachment_id, channel_id, analysis, created_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (attachment_id)
            DO UPDATE SET analysis = EXCLUDED.analysis
            """,
            attachment_id,
            channel_id,
            analysis,
        )


async def delete_image_analyses(channel_id: int, *, pool: Any | None = None) -> None:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM ticket_image_analyses WHERE channel_id = $1",
            channel_id,
        )


def render_transcript_text(lines: Sequence[TranscriptLine], *, channel_name: str) -> str:
    header = f"Transcript: #{channel_name}"
    entries = [
        f"[{line.created_at:%Y-%m-%d %H:%M} UTC] -- {line.content}"
        if line.speaker_kind == "system"
        else f"[{line.created_at:%Y-%m-%d %H:%M} UTC] {line.speaker_name} ({line.speaker_kind}): {line.content}"
        for line in lines
    ]
    return "\n".join([header, "=" * len(header), *entries]) + "\n"


def anonymize_lines(lines: Sequence[TranscriptLine]) -> list[TranscriptLine]:
    name_labels: dict[str, str] = {}
    for line in lines:
        if line.speaker_kind not in ("requester", "participant"):
            continue
        if len(line.speaker_name) < _MIN_ANONYMIZED_NAME_LENGTH:
            continue
        name_labels[line.speaker_name] = SPEAKER_LABELS.get(line.speaker_kind, "User")

    patterns = [
        (re.compile(rf"(?<!\w)@?{re.escape(name)}(?!\w)", re.IGNORECASE), label)
        for name, label in name_labels.items()
    ]

    anonymized: list[TranscriptLine] = []
    for line in lines:
        content = line.content
        for pattern, label in patterns:
            content = pattern.sub(label, content)
        anonymized.append(
            TranscriptLine(
                created_at=line.created_at,
                speaker_kind=line.speaker_kind,
                speaker_name=SPEAKER_LABELS.get(line.speaker_kind, "User"),
                content=content,
            )
        )
    return anonymized


def _normalize_summary_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = str(item or "").strip().lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tuple(tags[:5])


def _normalize_ticket_summary(payload: Any) -> TicketSummary:
    payload = payload if isinstance(payload, dict) else {}
    return TicketSummary(
        title=str(payload.get("title") or "").strip()[:80],
        problem=str(payload.get("problem") or "").strip(),
        resolution=str(payload.get("resolution") or "").strip(),
        resolved=bool(payload.get("resolved")),
        tags=_normalize_summary_tags(payload.get("tags")),
        knowledge_worthy=bool(payload.get("knowledge_worthy")),
        support_info=_normalize_suggestion(payload.get("support_info")),
    )


def _normalize_suggestion(value: Any) -> SupportInfoSuggestion | None:
    """Model output from member text, so mentions are defused; None when it doesn't pass validate(). Links stay in
    the body: staff see them on the review card before approving, and the answer is often the link itself."""
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip().lower()
    title = strip_links(defuse_mentions(str(value.get("title") or "")))
    body = defuse_mentions(str(value.get("body") or ""))
    try:
        title, body = support_info.validate(kind, title, body)
    except ValueError:
        return None
    return SupportInfoSuggestion(kind, title, body)


async def summarize_ticket(
    lines: Sequence[TranscriptLine],
    *,
    model: str,
    openai_client: Any | None = None,
    timeout_seconds: int = 60,
) -> TicketSummary | None:
    if not lines:
        return None

    anonymized = anonymize_lines(lines)
    transcript_text = "\n".join(f"{line.speaker_name}: {line.content}" for line in anonymized)
    transcript_text = transcript_text[-_TRANSCRIPT_TAIL_CHARS:]

    resolved_client = openai_client
    if resolved_client is None:
        from bulmaai.services.openai_client import client as resolved_client

    request_kwargs: dict[str, Any] = {
        "model": model,
        "instructions": TICKET_SUMMARY_INSTRUCTIONS,
        "input": [{"role": "user", "content": transcript_text}],
        "max_output_tokens": 1200,
        "metadata": {"app": "dragonminez-ai", "workflow": "ticket_summary"},
        "store": True,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "ticket_summary",
                "schema": TICKET_SUMMARY_SCHEMA,
                "strict": True,
            }
        },
    }
    if model.startswith("gpt-5"):
        request_kwargs["reasoning"] = {"effort": "low"}

    response = await asyncio.wait_for(
        resolved_client.responses.create(**request_kwargs),
        timeout=timeout_seconds,
    )
    ai_budget.record_response(response)
    return _normalize_ticket_summary(_extract_response_json(response))


def render_knowledge_markdown(
    summary: TicketSummary,
    lines: Sequence[TranscriptLine],
    *,
    closed_at: datetime,
) -> str:
    anonymized = anonymize_lines(lines)
    outcome = "Resolved" if summary.resolved else "Unresolved"
    header = [
        f"# Support ticket: {summary.title}",
        "",
        f"Source: DragonMineZ Discord support ticket (closed {closed_at:%Y-%m-%d})",
        f"Outcome: {outcome}",
        f"Tags: {', '.join(summary.tags)}",
        "",
        "## Problem",
        summary.problem,
        "",
        "## Resolution",
        summary.resolution,
        "",
        "## Conversation",
    ]
    # Member and AI lines stay out: members could plant fake fixes and links, AI lines quote account data.
    conversation = [
        f"**{line.speaker_name}:** {strip_links(defuse_mentions(line.content))}"
        for line in anonymized
        if line.speaker_kind == "staff"
    ]
    return "\n".join(header + conversation) + "\n"


def ticket_knowledge_filename(channel_id: int, closed_at: datetime) -> str:
    return f"ticket-{closed_at:%Y%m%d}-{channel_id}.md"


async def upload_ticket_knowledge(
    markdown: str,
    *,
    filename: str,
    vector_store_id: str,
    attributes: dict[str, str | bool | float],
    openai_client: Any | None = None,
) -> str:
    resolved_client = openai_client
    if resolved_client is None:
        from bulmaai.services.openai_client import client as resolved_client

    created_file = await resolved_client.files.create(
        file=(filename, markdown.encode("utf-8")),
        purpose="assistants",
    )
    file_id = str(getattr(created_file, "id"))
    await resolved_client.vector_stores.files.create_and_poll(
        vector_store_id=vector_store_id,
        file_id=file_id,
        attributes=attributes,
    )
    return file_id


async def has_transcript(channel_id: int, *, pool: Any | None = None) -> bool:
    resolved_pool = pool or await get_pool()
    return bool(await resolved_pool.fetchval("SELECT 1 FROM ticket_transcripts WHERE channel_id = $1 LIMIT 1", channel_id))


async def record_ticket_transcript(
    *,
    channel_id: int,
    guild_id: int | None,
    channel_name: str | None,
    requester_id: int | None,
    closed_by_id: int | None,
    resolved: bool,
    ai_confidence: float | None,
    message_count: int,
    summary: TicketSummary | None,
    transcript: str,
    openai_file_id: str | None,
    html_token: str | None = None,
    html_expires_at: datetime | None = None,
    pool: Any | None = None,
) -> None:
    """One row per channel: a re-opened ticket closes again with its full history and updates that row in place
    (same id; the page token comes from ticket_pages.save_page(reuse=...), so the address stays the same).
    A None openai_file_id keeps the file the first close taught; a None html_token (export failed) keeps the old page."""
    args = (
        channel_id,
        guild_id,
        channel_name,
        requester_id,
        closed_by_id,
        resolved,
        ai_confidence,
        message_count,
        summary.title if summary else None,
        summary.problem if summary else None,
        summary.resolution if summary else None,
        list(summary.tags) if summary else [],
        summary.knowledge_worthy if summary else False,
        transcript,
        openai_file_id,
        html_token,
        html_expires_at,
    )
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.execute(
                """
                UPDATE ticket_transcripts SET
                    guild_id = $2, channel_name = $3, requester_id = $4, closed_by_id = $5,
                    resolved = $6, ai_confidence = $7, message_count = $8, title = $9, problem = $10, resolution = $11,
                    tags = $12, knowledge_worthy = $13, transcript = $14, openai_file_id = COALESCE($15, openai_file_id),
                    html_token = COALESCE($16, html_token),
                    html_expires_at = CASE WHEN $16::text IS NULL THEN html_expires_at ELSE $17 END, closed_at = now()
                WHERE id = (SELECT max(id) FROM ticket_transcripts WHERE channel_id = $1)
                """,
                *args,
            )
            if updated.endswith(" 0"):
                await conn.execute(
                    """
                    INSERT INTO ticket_transcripts (
                        channel_id, guild_id, channel_name, requester_id, closed_by_id,
                        resolved, ai_confidence, message_count, title, problem, resolution,
                        tags, knowledge_worthy, transcript, openai_file_id, html_token, html_expires_at, closed_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, now())
                    """,
                    *args,
                )


async def knowledge_file_id(channel_id: int, *, pool: Any | None = None) -> str | None:
    """The vector-store file an earlier close of this channel uploaded; a re-close keeps it rather than adding a twin."""
    resolved_pool = pool or await get_pool()
    return await resolved_pool.fetchval(
        "SELECT openai_file_id FROM ticket_transcripts WHERE channel_id = $1 AND openai_file_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        channel_id,
    )
