from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool


@dataclass(slots=True, frozen=True)
class PatreonCampaignState:
    campaign_id: str
    last_processed_post_id: str | None
    last_processed_post_title: str | None
    last_processed_post_url: str | None
    last_processed_at: datetime | None


async def get_patreon_campaign_state(campaign_id: str) -> PatreonCampaignState | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                campaign_id,
                last_processed_post_id,
                last_processed_post_title,
                last_processed_post_url,
                last_processed_at
            FROM patreon_campaign_state
            WHERE campaign_id = $1
            """,
            campaign_id,
        )
    if row is None:
        return None

    return PatreonCampaignState(
        campaign_id=row["campaign_id"],
        last_processed_post_id=row["last_processed_post_id"],
        last_processed_post_title=row["last_processed_post_title"],
        last_processed_post_url=row["last_processed_post_url"],
        last_processed_at=row["last_processed_at"],
    )


async def upsert_patreon_campaign_state(
    *,
    campaign_id: str,
    post_id: str,
    post_title: str | None,
    post_url: str | None,
    published_at: datetime | None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO patreon_campaign_state (
                campaign_id,
                last_processed_post_id,
                last_processed_post_title,
                last_processed_post_url,
                last_processed_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, now())
            ON CONFLICT (campaign_id)
            DO UPDATE SET
                last_processed_post_id = EXCLUDED.last_processed_post_id,
                last_processed_post_title = EXCLUDED.last_processed_post_title,
                last_processed_post_url = EXCLUDED.last_processed_post_url,
                last_processed_at = EXCLUDED.last_processed_at,
                updated_at = now()
            """,
            campaign_id,
            post_id,
            post_title,
            post_url,
            published_at,
        )
