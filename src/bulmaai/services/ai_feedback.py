"""👍/👎 on Bulma's answers (ui/ai_feedback, table ai_feedback): one row per answer card that got the buttons, saved
with helpful NULL when the card is sent (it keeps the reply's response id for later study); the requester's first
click sets helpful. The weekly support report counts them (services/support_report).

The weekly "Answers to fix" review (cogs/support_info.py, posted right after the report) shows the week's 👎 answers
with their trace; the first staff click claims a row (reviewed_at), and a written fix keeps its support info id.

ponytail: rows are never purged; one small row per answer card."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bulmaai.database.db import get_pool
from bulmaai.services.support_faq import support_trace_to_faq_source
from bulmaai.services.support_traces import get_reply_traces


@dataclass(frozen=True)
class FeedbackRow:
    message_id: int
    channel_id: int
    requester_id: int
    response_id: str | None
    helpful: bool | None
    language: str
    created_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by: int | None = None
    info_id: int | None = None


@dataclass(frozen=True)
class AnswerToFix:
    """A 👎 answer for the weekly review: the requester's question and the reply they rated, from its trace."""

    message_id: int
    channel_id: int
    language: str
    created_at: datetime
    question: str
    reply: str


def _row(record: Any) -> FeedbackRow | None:
    if record is None:
        return None
    return FeedbackRow(
        message_id=record["message_id"],
        channel_id=record["channel_id"],
        requester_id=record["requester_id"],
        response_id=record["response_id"],
        helpful=record["helpful"],
        language=record["language"],
        created_at=record["created_at"],
        reviewed_at=record.get("reviewed_at"),
        reviewed_by=record.get("reviewed_by"),
        info_id=record.get("info_id"),
    )


async def save(
    *, message_id: int, channel_id: int, requester_id: int, response_id: str | None, language: str
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO ai_feedback (message_id, channel_id, requester_id, response_id, language)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (message_id) DO NOTHING
        """,
        message_id,
        channel_id,
        requester_id,
        response_id,
        language,
    )


async def get(message_id: int) -> FeedbackRow | None:
    pool = await get_pool()
    return _row(await pool.fetchrow("SELECT * FROM ai_feedback WHERE message_id = $1", message_id))


async def rate(*, message_id: int, channel_id: int, requester_id: int, helpful: bool) -> bool:
    """The first rating wins: False when the answer was already rated. A card whose row never got saved still
    records the rating (without its response id)."""
    pool = await get_pool()
    rated = await pool.fetchval(
        """
        INSERT INTO ai_feedback (message_id, channel_id, requester_id, helpful) VALUES ($1, $2, $3, $4)
        ON CONFLICT (message_id) DO UPDATE SET helpful = EXCLUDED.helpful WHERE ai_feedback.helpful IS NULL
        RETURNING message_id
        """,
        message_id,
        channel_id,
        requester_id,
        helpful,
    )
    return rated is not None


# ---- the weekly "Answers to fix" review ---------------------------------------------------------------------------


async def downvoted(since: datetime, until: datetime, *, limit: int = 10) -> list[FeedbackRow]:
    """Answers sent in [since, until) that their requester rated 👎 and nobody reviewed yet, newest first. Rows without
    a response id (a rating on a card whose row never got saved) have no trace to show."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM ai_feedback
        WHERE helpful = false AND response_id IS NOT NULL AND reviewed_at IS NULL
          AND created_at >= $1 AND created_at < $2
        ORDER BY created_at DESC LIMIT $3
        """,
        since,
        until,
        limit,
    )
    return [_row(row) for row in rows]


def _answer(row: FeedbackRow, trace: dict[str, Any]) -> AnswerToFix:
    source = support_trace_to_faq_source(trace)
    return AnswerToFix(
        message_id=row.message_id,
        channel_id=row.channel_id,
        language=row.language,
        created_at=row.created_at,
        question=source.question,
        reply=source.answer,
    )


async def answers_to_fix(since: datetime, until: datetime, *, limit: int = 10) -> list[AnswerToFix]:
    """downvoted() with each answer's question and reply; answers whose trace is gone are left out."""
    rows = await downvoted(since, until, limit=limit)
    traces = await get_reply_traces([row.response_id for row in rows])
    return [_answer(row, traces[row.response_id]) for row in rows if row.response_id in traces]


async def answer_to_fix(row: FeedbackRow) -> AnswerToFix | None:
    """One answer's question and reply (the Write form's title comes from the question)."""
    traces = await get_reply_traces([row.response_id] if row.response_id else [])
    return _answer(row, traces[row.response_id]) if row.response_id in traces else None


async def claim_review(message_id: int, staff_id: int) -> bool:
    """The first Write or Skip wins: False when someone already reviewed this answer."""
    pool = await get_pool()
    claimed = await pool.fetchval(
        """
        UPDATE ai_feedback SET reviewed_at = now(), reviewed_by = $2
        WHERE message_id = $1 AND reviewed_at IS NULL
        RETURNING message_id
        """,
        message_id,
        staff_id,
    )
    return claimed is not None


async def release_review(message_id: int, staff_id: int) -> None:
    """A write that didn't go through (the text didn't validate): the answer is open for review again. Only staff_id's
    own claim, and only while no support info came of it."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE ai_feedback SET reviewed_at = NULL, reviewed_by = NULL
        WHERE message_id = $1 AND reviewed_by = $2 AND info_id IS NULL
        """,
        message_id,
        staff_id,
    )


async def set_info(message_id: int, info_id: int) -> None:
    """The support info written from this answer."""
    pool = await get_pool()
    await pool.execute("UPDATE ai_feedback SET info_id = $2 WHERE message_id = $1", message_id, info_id)
