import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from bulmaai.database.db import get_pool

ALLOWED_FAQ_STATUSES = {"approved", "rejected"}
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-large"
EmbeddingProvider = Callable[[list[str], str], Awaitable[list[list[float]]]]


@dataclass(slots=True)
class FAQEntryInput:
    lang: str
    canonical_question: str
    answer: str
    tags: list[str] | None = None
    source_ticket_channel_id: int | None = None
    source_question_message_ids: list[int] | None = None
    source_answer_message_ids: list[int] | None = None
    approved_by: int | None = None

    def render_kwargs(self) -> dict[str, Any]:
        return {
            "canonical_question": self.canonical_question,
            "answer": self.answer,
            "tags": self.tags or [],
        }


@dataclass(slots=True)
class FAQUpsertResult:
    faq_id: int
    content_hash: str
    dimensions: int
    knowledge_base_version: int | None = None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_tags(tags: Sequence[str] | None, *, sort: bool = True) -> list[str]:
    if not tags:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        cleaned = _normalize_text(tag).lower()
        if cleaned and cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
    if sort:
        return sorted(normalized)
    return normalized


def validate_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized not in ALLOWED_FAQ_STATUSES:
        raise ValueError(f"Unsupported FAQ status: {status}")
    return normalized


def validate_lang(lang: str) -> str:
    normalized = lang.strip().lower()
    if not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", normalized):
        raise ValueError(f"Unsupported FAQ language code: {lang}")
    if len(normalized) > 5:
        raise ValueError(f"FAQ language code is too long: {lang}")
    return normalized


def render_faq_text(
    *,
    canonical_question: str,
    answer: str,
    tags: Sequence[str] | None = None,
) -> str:
    question = _normalize_text(canonical_question)
    rendered_answer = _normalize_text(answer)
    parts = [
        f"Question: {question}",
        f"Answer: {rendered_answer}",
    ]
    normalized_tags = _normalize_tags(tags, sort=False)
    if normalized_tags:
        parts.append(f"Tags: {', '.join(normalized_tags)}")
    return "\n\n".join(parts)


def content_hash(
    *,
    lang: str,
    canonical_question: str,
    answer: str,
    tags: Sequence[str] | None = None,
) -> str:
    payload = {
        "lang": validate_lang(lang),
        "question": _normalize_text(canonical_question),
        "answer": _normalize_text(answer),
        "tags": _normalize_tags(tags),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


async def bump_knowledge_base_version(
    *,
    name: str,
    conn: Any,
) -> None:
    await conn.execute(
        """
        INSERT INTO knowledge_base_version (name, version, updated_at)
        VALUES ($1, 1, now())
        ON CONFLICT (name)
        DO UPDATE SET
            version = knowledge_base_version.version + 1,
            updated_at = now()
        RETURNING version
        """,
        name,
    )


async def get_knowledge_base_version(
    name: str,
    *,
    pool: Any | None = None,
) -> int:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        version = await conn.fetchval(
            """
            SELECT version
            FROM knowledge_base_version
            WHERE name = $1
            """,
            name,
        )
    return int(version or 0)


async def upsert_approved_faq(
    entry: FAQEntryInput,
    *,
    embedding_provider: EmbeddingProvider,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    pool: Any | None = None,
) -> FAQUpsertResult:
    lang = validate_lang(entry.lang)
    question = _normalize_text(entry.canonical_question)
    answer = _normalize_text(entry.answer)
    tags = _normalize_tags(entry.tags, sort=False)
    if not question:
        raise ValueError("FAQ canonical question is required")
    if not answer:
        raise ValueError("FAQ answer is required")

    rendered = render_faq_text(
        canonical_question=question,
        answer=answer,
        tags=tags,
    )
    faq_hash = content_hash(
        lang=lang,
        canonical_question=question,
        answer=answer,
        tags=tags,
    )
    embeddings = await embedding_provider([rendered], embedding_model)
    if len(embeddings) != 1:
        raise ValueError("Embedding provider must return exactly one FAQ embedding")
    embedding = embeddings[0]

    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        async with conn.transaction():
            faq_id = await conn.fetchval(
                """
                INSERT INTO faq_entries (
                    status,
                    lang,
                    canonical_question,
                    answer,
                    tags,
                    source_ticket_channel_id,
                    source_question_message_ids,
                    source_answer_message_ids,
                    approved_by,
                    approved_at,
                    content_hash,
                    updated_at
                )
                VALUES ('approved', $1, $2, $3, $4, $5, $6, $7, $8, now(), $9, now())
                ON CONFLICT (lang, content_hash)
                DO UPDATE SET
                    status = 'approved',
                    canonical_question = EXCLUDED.canonical_question,
                    answer = EXCLUDED.answer,
                    tags = EXCLUDED.tags,
                    source_ticket_channel_id = EXCLUDED.source_ticket_channel_id,
                    source_question_message_ids = EXCLUDED.source_question_message_ids,
                    source_answer_message_ids = EXCLUDED.source_answer_message_ids,
                    approved_by = EXCLUDED.approved_by,
                    approved_at = EXCLUDED.approved_at,
                    rejected_by = NULL,
                    rejected_reason = NULL,
                    updated_at = now(),
                    version = faq_entries.version + 1
                RETURNING id
                """,
                lang,
                question,
                answer,
                tags,
                entry.source_ticket_channel_id,
                entry.source_question_message_ids or [],
                entry.source_answer_message_ids or [],
                entry.approved_by,
                faq_hash,
            )
            await conn.execute(
                """
                INSERT INTO faq_embeddings (faq_id, embedding, model, dimensions, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (faq_id)
                DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    model = EXCLUDED.model,
                    dimensions = EXCLUDED.dimensions,
                    updated_at = now()
                """,
                faq_id,
                embedding,
                embedding_model,
                len(embedding),
            )
            await bump_knowledge_base_version(name="faq", conn=conn)
            await conn.execute(
                """
                INSERT INTO faq_events (faq_id, event_type, actor_id, payload_json, created_at)
                VALUES ($1, $2, $3, $4::jsonb, now())
                """,
                faq_id,
                "approved_upsert",
                entry.approved_by,
                json.dumps(
                    {
                        "lang": lang,
                        "content_hash": faq_hash,
                        "source_ticket_channel_id": entry.source_ticket_channel_id,
                        "source_question_message_ids": entry.source_question_message_ids or [],
                        "source_answer_message_ids": entry.source_answer_message_ids or [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

    return FAQUpsertResult(
        faq_id=int(faq_id),
        content_hash=faq_hash,
        dimensions=len(embedding),
    )
