import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pymupdf
from openai import AsyncOpenAI

from bulmaai.config import load_settings
from bulmaai.database.db import get_pool

settings = load_settings()
log = logging.getLogger(__name__)
client = AsyncOpenAI(api_key=settings.openai_key)

CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP_CHARS = 160
EMBEDDING_BATCH = 64
SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".json", ".html", ".htm"}
TEXTISH_JSON_FIELDS = {
    "title",
    "name",
    "content",
    "description",
    "body",
    "text",
    "summary",
    "details",
    "notes",
    "steps",
}


@dataclass(slots=True)
class ChunkRecord:
    source: str
    source_type: str
    title: str
    content: str
    section: str | None
    chunk_index: int
    content_hash: str


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_source_type(filename: str, source_type: str = "auto") -> str:
    if source_type != "auto":
        return source_type
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix == ".json":
        return "json"
    return "text"


def extract_text_from_pdf_bytes(data: bytes) -> str:
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return "\n".join(pages)


def extract_text_from_html_bytes(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _json_object_to_sections(value: Any, path: str = "") -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, str]] = []
    if isinstance(value, dict):
        title = None
        text_parts: list[str] = []
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in {"title", "name"} and isinstance(nested, str):
                title = nested.strip() or title
            elif lowered in TEXTISH_JSON_FIELDS and isinstance(nested, str):
                cleaned = clean_text(nested)
                if cleaned:
                    text_parts.append(cleaned)
            else:
                child_path = f"{path}.{key}" if path else str(key)
                sections.extend(_json_object_to_sections(nested, child_path))
        if text_parts:
            section_title = title or path or None
            sections.insert(0, (section_title, "\n\n".join(text_parts)))
        return sections
    if isinstance(value, list):
        for index, nested in enumerate(value):
            child_path = f"{path}[{index}]"
            sections.extend(_json_object_to_sections(nested, child_path))
        return sections
    if isinstance(value, str):
        cleaned = clean_text(value)
        if cleaned:
            sections.append((path or None, cleaned))
    return sections


def extract_sections_from_json_bytes(data: bytes) -> list[tuple[str | None, str]]:
    parsed = json.loads(data.decode("utf-8", errors="replace"))
    sections = _json_object_to_sections(parsed)
    if sections:
        return sections
    fallback = clean_text(json.dumps(parsed, ensure_ascii=False, indent=2))
    return [(None, fallback)] if fallback else []


def extract_sections_from_text(text: str, source_type: str) -> list[tuple[str | None, str]]:
    cleaned = clean_text(text)
    if not cleaned:
        return []

    heading_matches = list(
        re.finditer(r"(?m)^(#{1,6}\s+.+|[A-Z][^\n]{2,80}:)$", cleaned)
    )
    if not heading_matches:
        return [(None, cleaned)]

    sections: list[tuple[str | None, str]] = []
    for index, match in enumerate(heading_matches):
        start = match.end()
        end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(cleaned)
        raw_heading = match.group(1).strip()
        heading = re.sub(r"^#{1,6}\s*", "", raw_heading).rstrip(":").strip()
        body = clean_text(cleaned[start:end])
        if body:
            sections.append((heading, body))
    return sections or [(None, cleaned)]


def extract_sections_from_bytes(data: bytes, filename: str, source_type: str = "auto") -> list[tuple[str | None, str]]:
    resolved_type = detect_source_type(filename, source_type)
    if resolved_type == "pdf":
        text = extract_text_from_pdf_bytes(data)
        return extract_sections_from_text(text, resolved_type)
    if resolved_type == "html":
        return extract_sections_from_text(extract_text_from_html_bytes(data), resolved_type)
    if resolved_type == "json":
        return extract_sections_from_json_bytes(data)
    text = data.decode("utf-8", errors="replace")
    return extract_sections_from_text(text, resolved_type)


def chunk_section_text(
    text: str,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            step = max(1, max_chars - overlap)
            for start in range(0, len(paragraph), step):
                chunks.append(paragraph[start:start + max_chars].strip())
            continue

        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current.strip())
            current = current[-overlap:] + "\n\n" + paragraph if overlap else paragraph
        else:
            current = f"{current}\n\n{paragraph}".strip() if current else paragraph

    if current:
        chunks.append(current.strip())

    return [chunk for chunk in chunks if chunk]


def build_chunk_records(
    *,
    sections: Iterable[tuple[str | None, str]],
    source: str,
    source_type: str,
) -> list[ChunkRecord]:
    records: list[ChunkRecord] = []
    chunk_index = 0
    for section_title, section_text in sections:
        for chunk in chunk_section_text(section_text):
            title = section_title or chunk.split("\n", 1)[0][:120].strip() or f"{source} chunk {chunk_index + 1}"
            content_hash = hashlib.sha256(
                f"{source}|{section_title or ''}|{chunk}".encode("utf-8")
            ).hexdigest()
            records.append(
                ChunkRecord(
                    source=source,
                    source_type=source_type,
                    title=title[:200],
                    content=chunk,
                    section=section_title,
                    chunk_index=chunk_index,
                    content_hash=content_hash,
                )
            )
            chunk_index += 1
    return records


def iter_supported_files(path: Path, *, recursive: bool = True) -> list[Path]:
    if path.is_file():
        return [path]
    pattern = "**/*" if recursive else "*"
    return sorted(
        [
            candidate
            for candidate in path.glob(pattern)
            if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    )


async def embed_texts(texts: list[str], embedding_model: str | None = None) -> list[list[float]]:
    model = embedding_model or settings.openai_embedding_model
    embeddings: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH):
        batch = texts[start:start + EMBEDDING_BATCH]
        log.info("Embedding chunk batch %d-%d / %d", start, start + len(batch) - 1, len(texts))
        resp = await client.embeddings.create(model=model, input=batch)
        embeddings.extend(item.embedding for item in resp.data)
    return embeddings


async def ensure_schema() -> None:
    schema_path = Path(__file__).resolve().parents[3] / "scripts" / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def delete_source(source: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("DELETE FROM docs WHERE source = $1 RETURNING id", source)
    return len(rows)


async def delete_source_prefix(prefix: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            DELETE FROM docs
            WHERE source = $1 OR source LIKE $1 || '/%'
            RETURNING id
            """,
            prefix,
        )
    return len(rows)


async def list_sources() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source, lang, source_type, count(*) AS chunks, min(created_at) AS first_upload, max(updated_at) AS last_update
            FROM docs
            GROUP BY source, lang, source_type
            ORDER BY source, lang
            """
        )
    return [dict(row) for row in rows]


async def upsert_chunks(
    *,
    chunks: list[ChunkRecord],
    embeddings: list[list[float]],
    lang: str,
    embedding_model: str | None = None,
) -> int:
    if len(chunks) != len(embeddings):
        raise ValueError("Chunk and embedding counts do not match")

    model = embedding_model or settings.openai_embedding_model
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for chunk, embedding in zip(chunks, embeddings):
                doc_id = await conn.fetchval(
                    """
                    INSERT INTO docs (source, source_type, section, chunk_index, title, content, content_hash, lang, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                    ON CONFLICT (source, lang, content_hash)
                    DO UPDATE SET
                        source_type = EXCLUDED.source_type,
                        section = EXCLUDED.section,
                        chunk_index = EXCLUDED.chunk_index,
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        updated_at = now()
                    RETURNING id
                    """,
                    chunk.source,
                    chunk.source_type,
                    chunk.section,
                    chunk.chunk_index,
                    chunk.title,
                    chunk.content,
                    chunk.content_hash,
                    lang,
                )
                await conn.execute(
                    """
                    INSERT INTO doc_embeddings (doc_id, embedding, model, dimensions)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (doc_id)
                    DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        model = EXCLUDED.model,
                        dimensions = EXCLUDED.dimensions
                    """,
                    doc_id,
                    embedding,
                    model,
                    len(embedding),
                )
    return len(chunks)


async def ingest_bytes(
    *,
    data: bytes,
    filename: str,
    lang: str,
    source: str,
    source_type: str = "auto",
    replace: bool = True,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    await ensure_schema()
    if replace:
        await delete_source(source)

    resolved_type = detect_source_type(filename, source_type)
    sections = extract_sections_from_bytes(data, filename, resolved_type)
    chunks = build_chunk_records(sections=sections, source=source, source_type=resolved_type)
    embeddings = await embed_texts([chunk.content for chunk in chunks], embedding_model=embedding_model)
    inserted = await upsert_chunks(chunks=chunks, embeddings=embeddings, lang=lang, embedding_model=embedding_model)
    return {
        "source": source,
        "source_type": resolved_type,
        "lang": lang,
        "chunks": inserted,
        "sections": len(sections),
    }


async def ingest_path(
    *,
    path: str,
    lang: str,
    source: str | None = None,
    replace: bool = True,
    recursive: bool = True,
    source_type: str = "auto",
    embedding_model: str | None = None,
) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(path)

    files = iter_supported_files(target, recursive=recursive)
    if not files:
        raise FileNotFoundError(f"No supported files found under {path}")

    results: list[dict[str, Any]] = []
    if target.is_dir() and replace and source:
        await delete_source_prefix(source)
        replace = False
    first = True
    for file_path in files:
        file_source = source or file_path.name
        if target.is_dir() and source:
            file_source = f"{source}/{file_path.relative_to(target).as_posix()}"
        data = file_path.read_bytes()
        result = await ingest_bytes(
            data=data,
            filename=file_path.name,
            lang=lang,
            source=file_source,
            source_type=source_type,
            replace=replace if first else False,
            embedding_model=embedding_model,
        )
        results.append(result)
        first = False
    return results
