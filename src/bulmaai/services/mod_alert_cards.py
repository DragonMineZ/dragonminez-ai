"""Full contents of a collapsed automod alert card, so "Show details" can bring them back (table mod_alert_cards)."""

import json

from bulmaai.database.db import get_pool


async def save(message_id: int, details: list[dict]) -> None:
    """Upsert the component dicts that make up the expanded card."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO mod_alert_cards (message_id, details) VALUES ($1, $2::jsonb)
        ON CONFLICT (message_id) DO UPDATE SET details = EXCLUDED.details
        """,
        message_id,
        json.dumps(details),
    )


async def load(message_id: int) -> list[dict] | None:
    pool = await get_pool()
    raw = await pool.fetchval("SELECT details FROM mod_alert_cards WHERE message_id = $1", message_id)
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw
