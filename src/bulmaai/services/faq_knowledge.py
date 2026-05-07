import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from bulmaai.database.db import get_pool

ALLOWED_FAQ_STATUSES = {"approved", "rejected"}
ALLOWED_FAQ_REVIEW_STATUSES = {"pending", "approved", "rejected"}
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


@dataclass(slots=True)
class FAQReviewCandidateInput:
    lang: str
    canonical_question: str
    answer: str
    tags: list[str] | None = None
    source_ticket_channel_id: int | None = None
    source_question_message_ids: list[int] | None = None
    source_answer_message_ids: list[int] | None = None
    proposed_by: int | None = None


@dataclass(slots=True)
class FAQReviewCandidate:
    id: int
    status: str
    lang: str
    canonical_question: str
    answer: str
    tags: list[str]
    source_ticket_channel_id: int | None
    source_question_message_ids: list[int]
    source_answer_message_ids: list[int]
    proposed_by: int | None
    reviewed_by: int | None
    review_reason: str | None
    approved_faq_id: int | None
    review_channel_id: int | None
    review_message_id: int | None
    created_at: Any | None
    updated_at: Any | None


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


def validate_review_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized not in ALLOWED_FAQ_REVIEW_STATUSES:
        raise ValueError(f"Unsupported FAQ review status: {status}")
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


def _candidate_from_row(row: Any) -> FAQReviewCandidate:
    return FAQReviewCandidate(
        id=int(row["id"]),
        status=str(row["status"]),
        lang=str(row["lang"]),
        canonical_question=str(row["canonical_question"]),
        answer=str(row["answer"]),
        tags=list(row["tags"] or []),
        source_ticket_channel_id=(
            int(row["source_ticket_channel_id"])
            if row["source_ticket_channel_id"] is not None
            else None
        ),
        source_question_message_ids=[int(value) for value in row["source_question_message_ids"] or []],
        source_answer_message_ids=[int(value) for value in row["source_answer_message_ids"] or []],
        proposed_by=int(row["proposed_by"]) if row["proposed_by"] is not None else None,
        reviewed_by=int(row["reviewed_by"]) if row["reviewed_by"] is not None else None,
        review_reason=row["review_reason"],
        approved_faq_id=int(row["approved_faq_id"]) if row["approved_faq_id"] is not None else None,
        review_channel_id=int(row["review_channel_id"]) if row["review_channel_id"] is not None else None,
        review_message_id=int(row["review_message_id"]) if row["review_message_id"] is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_faq_review_candidate(
    candidate: FAQReviewCandidateInput,
    *,
    pool: Any | None = None,
) -> int:
    lang = validate_lang(candidate.lang)
    question = _normalize_text(candidate.canonical_question)
    answer = _normalize_text(candidate.answer)
    tags = _normalize_tags(candidate.tags, sort=False)
    if not question:
        raise ValueError("FAQ review candidate question is required")
    if not answer:
        raise ValueError("FAQ review candidate answer is required")

    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        candidate_id = await conn.fetchval(
            """
            INSERT INTO faq_review_candidates (
                status,
                lang,
                canonical_question,
                answer,
                tags,
                source_ticket_channel_id,
                source_question_message_ids,
                source_answer_message_ids,
                proposed_by,
                created_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now(), now())
            RETURNING id
            """,
            "pending",
            lang,
            question,
            answer,
            tags,
            candidate.source_ticket_channel_id,
            candidate.source_question_message_ids or [],
            candidate.source_answer_message_ids or [],
            candidate.proposed_by,
        )
    return int(candidate_id)


async def get_faq_review_candidate(
    candidate_id: int,
    *,
    pool: Any | None = None,
) -> FAQReviewCandidate | None:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, status, lang, canonical_question, answer, tags,
                   source_ticket_channel_id, source_question_message_ids,
                   source_answer_message_ids, proposed_by, reviewed_by,
                   review_reason, approved_faq_id, review_channel_id,
                   review_message_id, created_at, updated_at
            FROM faq_review_candidates
            WHERE id = $1
            """,
            candidate_id,
        )
    return _candidate_from_row(row) if row else None


async def list_pending_faq_review_candidates(
    *,
    limit: int = 10,
    pool: Any | None = None,
) -> list[FAQReviewCandidate]:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, status, lang, canonical_question, answer, tags,
                   source_ticket_channel_id, source_question_message_ids,
                   source_answer_message_ids, proposed_by, reviewed_by,
                   review_reason, approved_faq_id, review_channel_id,
                   review_message_id, created_at, updated_at
            FROM faq_review_candidates
            WHERE status = 'pending'
            ORDER BY created_at DESC, id DESC
            LIMIT $1
            """,
            max(1, min(int(limit), 25)),
        )
    return [_candidate_from_row(row) for row in rows]


async def update_faq_review_message(
    candidate_id: int,
    *,
    channel_id: int,
    message_id: int,
    pool: Any | None = None,
) -> None:
    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE faq_review_candidates
            SET review_channel_id = $1,
                review_message_id = $2,
                updated_at = now()
            WHERE id = $3
            """,
            channel_id,
            message_id,
            candidate_id,
        )


async def reject_faq_candidate(
    candidate_id: int,
    *,
    actor_id: int,
    reason: str,
    pool: Any | None = None,
) -> None:
    cleaned_reason = _normalize_text(reason)
    if not cleaned_reason:
        raise ValueError("FAQ rejection reason is required")

    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE faq_review_candidates
            SET status = $1,
                reviewed_by = $2,
                review_reason = $3,
                updated_at = now()
            WHERE id = $4
            """,
            "rejected",
            actor_id,
            cleaned_reason,
            candidate_id,
        )


async def approve_faq_candidate(
    candidate_id: int,
    *,
    actor_id: int,
    embedding_provider: EmbeddingProvider,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    overrides: FAQReviewCandidateInput | None = None,
    pool: Any | None = None,
) -> FAQUpsertResult:
    candidate = await get_faq_review_candidate(candidate_id, pool=pool)
    if candidate is None:
        raise ValueError(f"FAQ review candidate not found: {candidate_id}")
    if candidate.status != "pending":
        raise ValueError(f"FAQ review candidate is already {candidate.status}")

    source = overrides or FAQReviewCandidateInput(
        lang=candidate.lang,
        canonical_question=candidate.canonical_question,
        answer=candidate.answer,
        tags=candidate.tags,
        source_ticket_channel_id=candidate.source_ticket_channel_id,
        source_question_message_ids=candidate.source_question_message_ids,
        source_answer_message_ids=candidate.source_answer_message_ids,
        proposed_by=candidate.proposed_by,
    )
    result = await upsert_approved_faq(
        entry=FAQEntryInput(
            lang=source.lang,
            canonical_question=source.canonical_question,
            answer=source.answer,
            tags=source.tags,
            source_ticket_channel_id=source.source_ticket_channel_id,
            source_question_message_ids=source.source_question_message_ids,
            source_answer_message_ids=source.source_answer_message_ids,
            approved_by=actor_id,
        ),
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        pool=pool,
    )

    resolved_pool = pool or await get_pool()
    async with resolved_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE faq_review_candidates
            SET status = $1,
                reviewed_by = $2,
                approved_faq_id = $3,
                updated_at = now()
            WHERE id = $4
            """,
            "approved",
            actor_id,
            result.faq_id,
            candidate_id,
        )
    return result


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
