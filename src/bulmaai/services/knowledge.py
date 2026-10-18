"""Support AI knowledge (`knowledge_docs` table): wiki pages, closed tickets, the generated FAQ and local notes,
searched with Postgres full-text search. The 'simple' tsquery lets Spanish/Portuguese questions still hit English
pages on shared terms (item names, "super saiyan").

ponytail: FTS, not embeddings; add pgvector only if answers measurably miss."""

import hashlib
import re
from pathlib import Path
from typing import Any

from bulmaai.database.db import get_pool

SOURCES = ("mediawiki", "ticket", "faq", "local")
SEARCH_TEXT_CHARS = 1500


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def upsert(
    filename: str,
    *,
    source: str,
    title: str,
    content: str,
    url: str | None = None,
    pool: Any | None = None,
) -> bool:
    """True when the row was inserted or changed; an identical doc is left alone."""
    resolved_pool = pool or await get_pool()
    status = await resolved_pool.execute(
        """
        INSERT INTO knowledge_docs (filename, source, title, url, content, content_sha256)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (filename) DO UPDATE SET
            source = EXCLUDED.source,
            title = EXCLUDED.title,
            url = EXCLUDED.url,
            content = EXCLUDED.content,
            content_sha256 = EXCLUDED.content_sha256,
            updated_at = now()
        WHERE (knowledge_docs.source, knowledge_docs.title, knowledge_docs.url, knowledge_docs.content_sha256)
            IS DISTINCT FROM (EXCLUDED.source, EXCLUDED.title, EXCLUDED.url, EXCLUDED.content_sha256)
        """,
        filename,
        source,
        title or "",
        url,
        content,
        content_sha256(content),
    )
    return str(status).endswith(" 1")


async def delete(filename: str, *, pool: Any | None = None) -> bool:
    resolved_pool = pool or await get_pool()
    status = await resolved_pool.execute("DELETE FROM knowledge_docs WHERE filename = $1", filename)
    return str(status).endswith(" 1")


async def list_by_source(source: str, *, pool: Any | None = None) -> dict[str, str]:
    """{filename: content_sha256} for every doc of one source."""
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        "SELECT filename, content_sha256 FROM knowledge_docs WHERE source = $1",
        source,
    )
    return {str(row["filename"]): str(row["content_sha256"]) for row in rows}


async def search(query: str, limit: int, *, pool: Any | None = None) -> list[dict[str, Any]]:
    """Best matches first: [{"filename", "title", "url", "text"}], text cut to SEARCH_TEXT_CHARS."""
    # websearch_to_tsquery ANDs every word, so a full question would match nothing: OR the words, rank sorts it out.
    query = " or ".join(word for word in re.findall(r"\w+", query or "") if len(word) > 1)
    if not query or limit <= 0:
        return []
    resolved_pool = pool or await get_pool()
    rows = await resolved_pool.fetch(
        """
        SELECT filename, title, url, left(content, $3) AS text
        FROM knowledge_docs, websearch_to_tsquery('english', $1) AS q
        WHERE tsv @@ q
        ORDER BY ts_rank_cd(tsv, q) DESC, filename
        LIMIT $2
        """,
        query,
        limit,
        SEARCH_TEXT_CHARS,
    )
    return [
        {
            "filename": str(row["filename"]),
            "title": str(row["title"] or ""),
            "url": row["url"],
            "text": str(row["text"] or "")[:SEARCH_TEXT_CHARS],
        }
        for row in rows
    ]


def local_doc_title(filename: str, content: str) -> str:
    """The first "# " heading, else the file stem."""
    for line in content.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return Path(filename).stem


async def sync_local_docs(directory: Path, *, pool: Any | None = None) -> dict[str, int]:
    """Mirror data/knowledge/*.md (README.md excluded) as source 'local'; rows whose file is gone are deleted."""
    files = sorted(
        path for path in Path(directory).glob("*.md") if path.is_file() and path.name.lower() != "readme.md"
    )
    stored = await list_by_source("local", pool=pool)
    uploaded = 0
    for path in files:
        content = path.read_text(encoding="utf-8")
        stored.pop(path.name, None)
        if await upsert(path.name, source="local", title=local_doc_title(path.name, content), content=content, pool=pool):
            uploaded += 1
    for filename in stored:
        await delete(filename, pool=pool)
    return {"uploaded": uploaded, "deleted": len(stored), "unchanged": len(files) - uploaded}
