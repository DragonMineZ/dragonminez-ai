import asyncio
import logging
import math
import re
import unicodedata
from collections import OrderedDict
from typing import Any, Literal

from openai import AsyncOpenAI

from bulmaai.config import load_settings
from bulmaai.database.db import get_pool

client = AsyncOpenAI(api_key=load_settings().openai_key)
log = logging.getLogger(__name__)

Embedding = list[float]
LangCode = Literal["en", "es", "pt"]

MIN_SIMILARITY = 0.42
MIN_RESULT_SCORE = 0.58
MAX_LEXICAL_CANDIDATES = 36
MAX_RECENT_CANDIDATES = 18
EMBED_CACHE_SIZE = 256
EMBED_CACHE: OrderedDict[str, Embedding] = OrderedDict()
SOURCE_TYPE_SCORE_BONUS = {
    "ticket_solution": 0.03,
}
RECENT_ONLY_PENALTY = 0.06


def _pick_doc_languages(user_lang: LangCode) -> tuple[str, ...]:
    if user_lang == "es":
        return ("es", "en")
    if user_lang == "pt":
        return ("pt", "en")
    return ("en",)


def _normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _normalize_query(text: str) -> str:
    return re.sub(r"\s+", " ", _normalize_text(text).strip())


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_]{3,}", _normalize_text(text))
        if token not in {"with", "from", "that", "this", "have", "about", "para", "como", "where"}
    }


def _cosine_similarity(a: Embedding, b: Embedding) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _keyword_overlap_score(query_tokens: set[str], title: str, content: str) -> float:
    if not query_tokens:
        return 0.0
    doc_tokens = _tokenize(f"{title} {content[:500]}")
    if not doc_tokens:
        return 0.0
    overlap = len(query_tokens & doc_tokens)
    return overlap / max(1, len(query_tokens))


def _trim_content(content: str, limit: int = 700) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rsplit(" ", 1)[0] + "..."


async def _get_query_embedding(text: str) -> Embedding:
    settings = load_settings()
    cache_key = _normalize_query(text)
    if cache_key in EMBED_CACHE:
        EMBED_CACHE.move_to_end(cache_key)
        return EMBED_CACHE[cache_key]

    res = await asyncio.wait_for(
        client.embeddings.create(
            model=settings.openai_embedding_model,
            input=text,
        ),
        timeout=settings.ai_support_timeout_seconds,
    )
    embedding = res.data[0].embedding
    EMBED_CACHE[cache_key] = embedding
    EMBED_CACHE.move_to_end(cache_key)
    while len(EMBED_CACHE) > EMBED_CACHE_SIZE:
        EMBED_CACHE.popitem(last=False)
    return embedding


async def _fetch_candidate_docs(
    *,
    query: str,
    doc_languages: tuple[str, ...],
    lexical_limit: int = MAX_LEXICAL_CANDIDATES,
    recent_limit: int = MAX_RECENT_CANDIDATES,
) -> list[dict[str, Any]]:
    pool = await get_pool()
    query_text = re.sub(r"\s+", " ", query).strip() or "support"
    candidates: OrderedDict[int, dict[str, Any]] = OrderedDict()

    async with pool.acquire() as conn:
        lexical_rows = await conn.fetch(
            """
            WITH search_query AS (
                SELECT websearch_to_tsquery('simple', $1) AS q
            )
            SELECT d.id, d.source, d.source_type, d.section, d.title, d.content, d.lang, d.updated_at,
                   e.embedding,
                   ts_rank_cd(
                       to_tsvector('simple', coalesce(d.title, '') || ' ' || coalesce(d.content, '')),
                       search_query.q
                   ) AS lexical_rank
            FROM docs AS d
            JOIN doc_embeddings AS e ON e.doc_id = d.id
            CROSS JOIN search_query
            WHERE d.lang = ANY($2::text[])
              AND to_tsvector('simple', coalesce(d.title, '') || ' ' || coalesce(d.content, '')) @@ search_query.q
            ORDER BY lexical_rank DESC, d.updated_at DESC
            LIMIT $3
            """,
            query_text,
            list(doc_languages),
            lexical_limit,
        )

        recent_rows = await conn.fetch(
            """
            SELECT d.id, d.source, d.source_type, d.section, d.title, d.content, d.lang, d.updated_at,
                   e.embedding,
                   0.0::float8 AS lexical_rank
            FROM docs AS d
            JOIN doc_embeddings AS e ON e.doc_id = d.id
            WHERE d.lang = ANY($1::text[])
            ORDER BY d.updated_at DESC, d.id DESC
            LIMIT $2
            """,
            list(doc_languages),
            recent_limit,
        )

    for row in [*lexical_rows, *recent_rows]:
        row_dict = dict(row)
        candidates.setdefault(row_dict["id"], row_dict)

    return list(candidates.values())


def _confidence_label(best_score: float) -> str:
    if best_score >= 0.8:
        return "high"
    if best_score >= 0.6:
        return "medium"
    return "low"


async def run_docs_search(
    query: str,
    language: LangCode = "en",
    max_results: int = 5,
    _bot_context: Any = None,
) -> dict[str, Any]:
    doc_languages = _pick_doc_languages(language)
    query_embedding = await _get_query_embedding(query)
    query_tokens = _tokenize(query)
    candidates = await _fetch_candidate_docs(query=query, doc_languages=doc_languages)

    if not candidates:
        return {
            "matches": [],
            "suggested_answers": [],
            "best_similarity": 0.0,
            "best_score": 0.0,
            "similarity_threshold": MIN_SIMILARITY,
            "used_languages": list(doc_languages),
            "query": query,
            "confidence": "low",
        }

    lexical_max = max((float(candidate["lexical_rank"] or 0.0) for candidate in candidates), default=0.0)

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        semantic_similarity = _cosine_similarity(query_embedding, candidate["embedding"])
        lexical_rank = float(candidate["lexical_rank"] or 0.0)
        lexical_score = lexical_rank / lexical_max if lexical_max > 0 else 0.0
        keyword_score = _keyword_overlap_score(query_tokens, candidate["title"], candidate["content"])
        hybrid_score = (semantic_similarity * 0.72) + (lexical_score * 0.18) + (keyword_score * 0.10)
        hybrid_score += SOURCE_TYPE_SCORE_BONUS.get(candidate["source_type"], 0.0)
        if lexical_rank == 0.0 and keyword_score == 0.0:
            hybrid_score -= RECENT_ONLY_PENALTY
        scored.append(
            {
                "id": candidate["id"],
                "source": candidate["source"],
                "source_type": candidate["source_type"],
                "section": candidate["section"],
                "title": candidate["title"],
                "content": candidate["content"],
                "similarity": semantic_similarity,
                "lexical_rank": lexical_rank,
                "keyword_score": keyword_score,
                "score": hybrid_score,
                "lang": candidate["lang"],
            }
        )

    scored.sort(key=lambda item: (item["score"], item["similarity"], item["lexical_rank"]), reverse=True)

    matches: list[dict[str, Any]] = []
    for item in scored:
        if item["similarity"] < MIN_SIMILARITY and item["score"] < MIN_RESULT_SCORE:
            continue
        matches.append(
            {
                "title": item["title"],
                "content": _trim_content(item["content"]),
                "source": item["source"],
                "section": item["section"],
                "lang": item["lang"],
                "source_type": item["source_type"],
                "similarity": item["similarity"],
                "score": item["score"],
                "keyword_score": item["keyword_score"],
            }
        )
        if len(matches) >= max_results:
            break

    best_similarity = float(scored[0]["similarity"])
    best_score = float(scored[0]["score"])
    confidence = _confidence_label(best_score)

    suggested_answers = [
        {
            "title": match["title"],
            "answer": match["content"],
            "source": match["source"],
            "section": match["section"],
            "score": match["score"],
        }
        for match in matches[:3]
    ]

    log.info(
        "docs_search best_similarity=%.4f best_score=%.4f threshold=%.2f candidates=%d languages=%s",
        best_similarity,
        best_score,
        MIN_SIMILARITY,
        len(candidates),
        ",".join(doc_languages),
    )

    return {
        "matches": matches,
        "suggested_answers": suggested_answers,
        "best_similarity": best_similarity,
        "best_score": best_score,
        "similarity_threshold": MIN_SIMILARITY,
        "used_languages": list(doc_languages),
        "query": query,
        "confidence": confidence,
    }
