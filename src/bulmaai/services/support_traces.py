import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bulmaai.database.db import get_pool


@dataclass(frozen=True, slots=True)
class SupportSession:
    channel_id: int
    openai_conversation_id: str
    last_response_id: str | None = None


@dataclass(frozen=True, slots=True)
class SupportAITrace:
    workflow: str
    response_id: str | None
    openai_conversation_id: str | None
    previous_response_id: str | None
    model: str
    language: str
    channel_id: int
    user_id: int
    prompt_cache_key: str | None
    file_search_enabled: bool
    vector_store_ids: Sequence[str]
    tool_names: Sequence[str]
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    reply_text: str
    input_json: Any
    request_metadata: dict[str, Any]


async def get_support_session(
    channel_id: int,
    *,
    pool: Any | None = None,
) -> SupportSession | None:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT channel_id, openai_conversation_id, last_response_id
            FROM support_sessions
            WHERE channel_id = $1
            """,
            channel_id,
        )
    if row is None:
        return None
    return SupportSession(
        channel_id=int(row["channel_id"]),
        openai_conversation_id=str(row["openai_conversation_id"]),
        last_response_id=row["last_response_id"],
    )


async def upsert_support_session(
    *,
    channel_id: int,
    openai_conversation_id: str,
    last_response_id: str | None,
    pool: Any | None = None,
) -> None:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO support_sessions (
                channel_id,
                openai_conversation_id,
                last_response_id,
                created_at,
                updated_at
            )
            VALUES ($1, $2, $3, now(), now())
            ON CONFLICT (channel_id)
            DO UPDATE SET
                openai_conversation_id = EXCLUDED.openai_conversation_id,
                last_response_id = EXCLUDED.last_response_id,
                updated_at = now()
            """,
            channel_id,
            openai_conversation_id,
            last_response_id,
        )


async def record_support_ai_trace(
    trace: SupportAITrace,
    *,
    pool: Any | None = None,
) -> None:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO support_ai_traces (
                workflow,
                response_id,
                openai_conversation_id,
                previous_response_id,
                model,
                language,
                channel_id,
                user_id,
                prompt_cache_key,
                file_search_enabled,
                vector_store_ids,
                tool_names,
                latency_ms,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                reasoning_tokens,
                reply_text,
                input_json,
                request_metadata,
                created_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18,
                $19, $20::jsonb, $21::jsonb, now()
            )
            """,
            trace.workflow,
            trace.response_id,
            trace.openai_conversation_id,
            trace.previous_response_id,
            trace.model,
            trace.language,
            trace.channel_id,
            trace.user_id,
            trace.prompt_cache_key,
            trace.file_search_enabled,
            list(trace.vector_store_ids),
            list(trace.tool_names),
            trace.latency_ms,
            trace.input_tokens,
            trace.output_tokens,
            trace.total_tokens,
            trace.cached_tokens,
            trace.reasoning_tokens,
            trace.reply_text,
            json.dumps(trace.input_json, ensure_ascii=False),
            json.dumps(trace.request_metadata, ensure_ascii=False, sort_keys=True),
        )


async def list_support_eval_trace_rows(
    *,
    limit: int = 200,
    pool: Any | None = None,
) -> list[Any]:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, created_at, response_id, model, language, channel_id, user_id,
                   tool_names, reply_text, input_json
            FROM support_ai_traces
            WHERE workflow = 'support_question'
              AND coalesce(reply_text, '') <> ''
              AND coalesce(reply_text, '') <> '(no reply)'
            ORDER BY created_at DESC
            LIMIT $1
            """,
            max(1, min(int(limit), 5000)),
        )
    return list(rows)


def support_trace_to_eval_row(row: Any) -> dict[str, Any]:
    trace_id = int(row["id"])
    input_json = row["input_json"] or []
    return {
        "custom_id": f"support-trace-{trace_id}",
        "input": input_json,
        "ideal": row["reply_text"] or "",
        "metadata": {
            "trace_id": trace_id,
            "created_at": str(row["created_at"]),
            "response_id": row["response_id"],
            "model": row["model"],
            "language": row["language"],
            "channel_id": str(row["channel_id"]) if row["channel_id"] is not None else None,
            "user_id": str(row["user_id"]) if row["user_id"] is not None else None,
            "tool_names": list(row["tool_names"] or []),
        },
    }


def write_eval_jsonl(rows: Iterable[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
