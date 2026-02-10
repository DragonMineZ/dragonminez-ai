import math
from typing import Any, Dict, List, Literal, Tuple

from openai import OpenAI

from src.bulmaai.database.db import get_pool

client = OpenAI()

Embedding = List[float]
LangCode = Literal["en", "es", "pt"]


def _pick_doc_language(user_lang: LangCode) -> str:
    """
    Map user language to documentation language.
    Currently docs are EN + ES; PT falls back to EN.
    """
    if user_lang == "es":
        return "es"
    return "en"


def _cosine_similarity(a: Embedding, b: Embedding) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _get_query_embedding(text: str) -> Embedding:
    """
    Get an embedding for the query using OpenAI embeddings API. [web:274]
    """
    res = client.embeddings.create(
        model="text-embedding-3-small",
        input=text,
    )
    # responses API returns .data list with embedding vectors
    return res.data[0].embedding  # type: ignore[no-any-return]


async def _fetch_candidate_docs(doc_lang: str, limit: int = 200) -> List[Tuple[int, str, str, List[float]]]:
    """
    Fetch a subset of docs + their embeddings from Postgres.
    Returns list of (doc_id, title, content, embedding)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.title, d.content, e.embedding
            FROM docs AS d
            JOIN doc_embeddings AS e ON e.doc_id = d.id
            WHERE d.lang = $1
            LIMIT $2
            """,
            doc_lang,
            limit,
        )
    result: List[Tuple[int, str, str, List[float]]] = []
    for r in rows:
        # asyncpg returns list for array field
        result.append((r["id"], r["title"], r["content"], r["embedding"]))
    return result


async def run_docs_search(
    query: str,
    language: LangCode = "en",
    max_results: int = 5,
) -> Dict[str, Any]:
    """
    Tool implementation for 'docs_search'.

    Returns a dict that the model will see as JSON:
    {
      "matches": [
        {"title": "...", "content": "...", "similarity": 0.91},
        ...
      ],
      "best_similarity": 0.91
    }
    """
    # 1) Map user language to doc language
    doc_lang = _pick_doc_language(language)

    # 2) Embed query
    query_embedding = await _get_query_embedding(query)

    # 3) Load candidate docs from DB
    candidates = await _fetch_candidate_docs(doc_lang, limit=300)

    scored: List[Tuple[float, str, str]] = []
    for _doc_id, title, content, emb in candidates:
        sim = _cosine_similarity(query_embedding, emb)
        scored.append((sim, title, content))

    # 4) Sort by similarity
    scored.sort(key=lambda x: x[0], reverse=True)

    # 5) Build result list with a similarity threshold
    THRESHOLD = 0.70  # tweak later
    matches = []
    for sim, title, content in scored[: max_results * 2]:  # pick a bit more, then filter
        if sim < THRESHOLD:
            continue
        matches.append(
            {
                "title": title,
                "content": content,
                "similarity": sim,
            }
        )
        if len(matches) >= max_results:
            break

    best_sim = scored[0][0] if scored else 0.0

    return {
        "matches": matches,
        "best_similarity": best_sim,
        "used_language": doc_lang,
        "query": query,
    }
