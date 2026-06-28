from dataclasses import dataclass
from datetime import datetime

from bulmaai.database.db import get_pool


@dataclass(slots=True, frozen=True)
class BugReport:
    thread_id: int
    guild_id: int | None
    reporter_id: int | None
    triage_message_id: int | None
    repo: str | None
    issue_number: int | None
    status: str
    ai_title: str | None
    ai_summary: str | None
    created_at: datetime | None
    updated_at: datetime | None


def _row_to_bug_report(row) -> BugReport:
    return BugReport(
        thread_id=int(row["thread_id"]),
        guild_id=row["guild_id"],
        reporter_id=row["reporter_id"],
        triage_message_id=row["triage_message_id"],
        repo=row["repo"],
        issue_number=row["issue_number"],
        status=row["status"],
        ai_title=row["ai_title"],
        ai_summary=row["ai_summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_bug_report(thread_id: int) -> BugReport | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM bug_reports WHERE thread_id = $1",
            thread_id,
        )
    return _row_to_bug_report(row) if row is not None else None


async def upsert_triage(
    *,
    thread_id: int,
    guild_id: int | None,
    reporter_id: int | None,
    triage_message_id: int | None,
    ai_title: str | None,
    ai_summary: str | None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bug_reports (
                thread_id, guild_id, reporter_id, triage_message_id,
                status, ai_title, ai_summary, updated_at
            )
            VALUES ($1, $2, $3, $4, 'triaged', $5, $6, now())
            ON CONFLICT (thread_id) DO UPDATE SET
                triage_message_id = EXCLUDED.triage_message_id,
                ai_title = EXCLUDED.ai_title,
                ai_summary = EXCLUDED.ai_summary,
                updated_at = now()
            """,
            thread_id,
            guild_id,
            reporter_id,
            triage_message_id,
            ai_title,
            ai_summary,
        )


async def set_tracked(thread_id: int, *, repo: str, issue_number: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE bug_reports
            SET repo = $2, issue_number = $3, status = 'tracked', updated_at = now()
            WHERE thread_id = $1
            """,
            thread_id,
            repo,
            issue_number,
        )


async def set_status(thread_id: int, status: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE bug_reports SET status = $2, updated_at = now() WHERE thread_id = $1",
            thread_id,
            status,
        )


async def list_tracked() -> list[BugReport]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM bug_reports
            WHERE status = 'tracked' AND issue_number IS NOT NULL
            ORDER BY updated_at ASC
            """
        )
    return [_row_to_bug_report(row) for row in rows]
