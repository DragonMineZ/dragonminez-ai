import json
from dataclasses import dataclass
from typing import Any

from bulmaai.database.db import get_pool
from bulmaai.services.dev_jar_downloads import DevJarArtifact, DevJarCommit


@dataclass(frozen=True, slots=True)
class PendingDevJarReview:
    channel_id: int | None
    message_id: int | None
    artifact: DevJarArtifact
    sha256: str | None
    workflow_run_url: str | None
    commits: tuple[DevJarCommit, ...]


def _commit_to_json(commit: DevJarCommit) -> dict[str, Any]:
    return {
        "sha": commit.sha,
        "title": commit.title,
        "description": commit.description,
        "author": commit.author,
        "url": commit.url,
    }


def _commit_from_json(data: dict[str, Any]) -> DevJarCommit:
    return DevJarCommit(
        sha=data["sha"],
        title=data["title"],
        description=data.get("description"),
        author=data["author"],
        url=data["url"],
    )


async def get_pending_dev_jar_review() -> PendingDevJarReview | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM dev_jar_pending_review WHERE id = 1")
    if row is None:
        return None

    commits = tuple(_commit_from_json(item) for item in json.loads(row["commits"]))
    artifact = DevJarArtifact(
        file_name=row["artifact_file_name"],
        version=row["artifact_version"],
        commit_sha=row["artifact_commit_sha"],
    )
    return PendingDevJarReview(
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        artifact=artifact,
        sha256=row["artifact_sha256"],
        workflow_run_url=row["workflow_run_url"],
        commits=commits,
    )


async def upsert_pending_dev_jar_review(
    *,
    artifact: DevJarArtifact,
    sha256: str | None,
    workflow_run_url: str | None,
    commits: tuple[DevJarCommit, ...],
    channel_id: int | None = None,
    message_id: int | None = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO dev_jar_pending_review (
                id, channel_id, message_id, artifact_file_name, artifact_version,
                artifact_commit_sha, artifact_sha256, workflow_run_url, commits, updated_at
            )
            VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8::jsonb, now())
            ON CONFLICT (id) DO UPDATE SET
                channel_id = EXCLUDED.channel_id,
                message_id = EXCLUDED.message_id,
                artifact_file_name = EXCLUDED.artifact_file_name,
                artifact_version = EXCLUDED.artifact_version,
                artifact_commit_sha = EXCLUDED.artifact_commit_sha,
                artifact_sha256 = EXCLUDED.artifact_sha256,
                workflow_run_url = EXCLUDED.workflow_run_url,
                commits = EXCLUDED.commits,
                updated_at = now()
            """,
            channel_id,
            message_id,
            artifact.file_name,
            artifact.version,
            artifact.commit_sha,
            sha256,
            workflow_run_url,
            json.dumps([_commit_to_json(commit) for commit in commits]),
        )


async def set_pending_dev_jar_review_message(channel_id: int, message_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE dev_jar_pending_review
            SET channel_id = $1, message_id = $2, updated_at = now()
            WHERE id = 1
            """,
            channel_id,
            message_id,
        )


async def clear_pending_dev_jar_review() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM dev_jar_pending_review WHERE id = 1")
