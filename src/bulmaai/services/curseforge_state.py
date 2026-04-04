from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool
from bulmaai.services.curseforge_client import CurseForgeRelease


@dataclass(slots=True, frozen=True)
class CurseForgeProjectState:
    project_id: int
    project_slug: str
    last_processed_file_id: int | None
    last_processed_file_name: str | None
    last_processed_file_url: str | None
    last_processed_at: datetime | None


async def get_curseforge_project_state(project_id: int) -> CurseForgeProjectState | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                project_id,
                project_slug,
                last_processed_file_id,
                last_processed_file_name,
                last_processed_file_url,
                last_processed_at
            FROM curseforge_project_state
            WHERE project_id = $1
            """,
            project_id,
        )
    if row is None:
        return None

    return CurseForgeProjectState(
        project_id=int(row["project_id"]),
        project_slug=row["project_slug"],
        last_processed_file_id=row["last_processed_file_id"],
        last_processed_file_name=row["last_processed_file_name"],
        last_processed_file_url=row["last_processed_file_url"],
        last_processed_at=row["last_processed_at"],
    )


async def upsert_curseforge_project_state(release: CurseForgeRelease) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO curseforge_project_state (
                project_id,
                project_slug,
                last_processed_file_id,
                last_processed_file_name,
                last_processed_file_url,
                last_processed_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (project_id)
            DO UPDATE SET
                project_slug = EXCLUDED.project_slug,
                last_processed_file_id = EXCLUDED.last_processed_file_id,
                last_processed_file_name = EXCLUDED.last_processed_file_name,
                last_processed_file_url = EXCLUDED.last_processed_file_url,
                last_processed_at = EXCLUDED.last_processed_at,
                updated_at = now()
            """,
            release.project_id,
            release.project_slug,
            release.file_id,
            release.file_display_name,
            release.file_page_url,
            release.uploaded_at,
        )
