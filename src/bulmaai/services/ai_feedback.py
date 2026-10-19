"""👍/👎 on Bulma's answers (ui/ai_feedback, table ai_feedback): one row per answer card that got the buttons, saved
with helpful NULL when the card is sent (it keeps the reply's response id for later study); the requester's first
click sets helpful. The weekly support report counts them (services/support_report).

ponytail: rows are never purged; one small row per answer card."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bulmaai.database.db import get_pool


@dataclass(frozen=True)
class FeedbackRow:
    message_id: int
    channel_id: int
    requester_id: int
    response_id: str | None
    helpful: bool | None
    language: str
    created_at: datetime


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
