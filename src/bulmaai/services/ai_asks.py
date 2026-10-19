"""One row per answer card that carries Bulma's controls (ui/ai_asks, table ai_asks): the validated spec the card was
built from, read by clicks and form opens (so they work after a restart), and claimed by the first answer.

ponytail: rows are never purged; one small row per question card."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bulmaai.database.db import get_pool


@dataclass(frozen=True)
class AskRow:
    message_id: int
    channel_id: int
    requester_id: int
    spec: dict[str, Any]
    language: str
    created_at: datetime
    answered_at: datetime | None


def _row(record: Any) -> AskRow | None:
    if record is None:
        return None
    spec = record["spec"]
    return AskRow(
        message_id=record["message_id"],
        channel_id=record["channel_id"],
        requester_id=record["requester_id"],
        spec=json.loads(spec) if isinstance(spec, str) else dict(spec),
        language=record["language"],
        created_at=record["created_at"],
        answered_at=record["answered_at"],
    )


async def save(*, message_id: int, channel_id: int, requester_id: int, spec: dict[str, Any], language: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO ai_asks (message_id, channel_id, requester_id, spec, language)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        ON CONFLICT (message_id) DO NOTHING
        """,
        message_id,
        channel_id,
        requester_id,
        json.dumps(spec, ensure_ascii=False),
        language,
    )


async def get(message_id: int) -> AskRow | None:
    pool = await get_pool()
    return _row(await pool.fetchrow("SELECT * FROM ai_asks WHERE message_id = $1", message_id))


async def claim(message_id: int) -> AskRow | None:
    """The first answer wins: None when the card was already answered (or has no row)."""
    pool = await get_pool()
    return _row(
        await pool.fetchrow(
            "UPDATE ai_asks SET answered_at = now() WHERE message_id = $1 AND answered_at IS NULL RETURNING *",
            message_id,
        )
    )


async def release(message_id: int) -> None:
    """The claimed answer never made it onto the card: the question is open again."""
    pool = await get_pool()
    await pool.execute("UPDATE ai_asks SET answered_at = NULL WHERE message_id = $1", message_id)
