#!/usr/bin/env python3
"""
Ingest a PDF into the docs database for RAG search.

Designed for **repeatable** uploads:
  • Each PDF is tracked by a *source* name (defaults to the filename).
  • Running the script again with the same source will DELETE the old
    chunks and re-insert fresh ones (replace is the default).
  • Use --append to add chunks without touching existing ones.
  • Use the "list" command to see what's already in the database.
  • Use the "delete" command to remove a source without adding anything.

Usage examples:
    # First upload (English)
    python scripts/ingest_pdf.py upload data/wiki_en.pdf --lang en

    # Re-upload after the PDF was updated – old chunks are replaced
    python scripts/ingest_pdf.py upload data/wiki_en.pdf --lang en

    # Upload with a custom source name
    python scripts/ingest_pdf.py upload data/guide_v2.pdf --lang en --source guide

    # Append without removing old data
    python scripts/ingest_pdf.py upload data/extra.pdf --lang en --append

    # See what sources exist
    python scripts/ingest_pdf.py list

    # Remove a source entirely
    python scripts/ingest_pdf.py delete wiki_en.pdf

Requirements (one-time):
    pip install pymupdf openai asyncpg python-dotenv
"""

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
from dotenv import load_dotenv
from openai import AsyncOpenAI

# ── allow imports from the project ──────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bulmaai.config import load_settings
from bulmaai.database.db import init_db_pool, close_db_pool, get_pool

load_dotenv()
settings = load_settings()
client = AsyncOpenAI(api_key=settings.openai_key)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

# ── tunables ────────────────────────────────────────────────────────
CHUNK_MAX_CHARS = 1500       # target max characters per chunk
CHUNK_OVERLAP_CHARS = 200    # overlap between consecutive chunks
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_BATCH = 50         # how many texts to embed in one API call


# ═══════════════════════════════════════════════════════════════════
# PDF → text
# ═══════════════════════════════════════════════════════════════════

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract all text from a PDF using PyMuPDF."""
    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()
    return "\n".join(pages)


def clean_text(text: str) -> str:
    """Basic cleanup: collapse whitespace, fix common artefacts."""
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════
# Chunking
# ═══════════════════════════════════════════════════════════════════

def chunk_text(
    text: str,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """
    Split *text* into overlapping chunks on paragraph boundaries.
    Falls back to hard character splits for very long paragraphs.
    """
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(para) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for i in range(0, len(para), max_chars - overlap):
                chunks.append(para[i : i + max_chars].strip())
            continue

        if len(current) + len(para) + 2 > max_chars:
            chunks.append(current.strip())
            current = current[-overlap:] + "\n\n" + para if overlap else para
        else:
            current = (current + "\n\n" + para) if current else para

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


def derive_title(chunk: str, index: int) -> str:
    """Use the first non-empty line as the title (truncated)."""
    first_line = chunk.split("\n", 1)[0].strip()
    if first_line:
        return first_line[:120]
    return f"Chunk {index}"


# ═══════════════════════════════════════════════════════════════════
# Embeddings
# ═══════════════════════════════════════════════════════════════════

async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts in batches via OpenAI."""
    all_embeddings: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH):
        batch = texts[start : start + EMBEDDING_BATCH]
        log.info(
            "  embedding batch %d–%d / %d",
            start, start + len(batch) - 1, len(texts),
        )
        resp = await client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        for item in resp.data:
            all_embeddings.append(item.embedding)
    return all_embeddings


# ═══════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════

async def ensure_schema():
    """Create tables/indexes if they don't already exist."""
    pool = await get_pool()
    schema_path = Path(__file__).with_name("schema.sql")
    sql = schema_path.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    log.info("  schema OK")


async def delete_source(source: str) -> int:
    """Delete all docs (and their embeddings via CASCADE) for a source."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "DELETE FROM docs WHERE source = $1 RETURNING id",
            source,
        )
    return len(rows)


async def list_sources():
    """Print every source currently in the database."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source, lang, count(*) AS chunks,
                   min(created_at) AS first_upload
            FROM docs
            GROUP BY source, lang
            ORDER BY source, lang
            """
        )
    if not rows:
        print("(no documents in the database)")
        return
    print(f"{'SOURCE':<40} {'LANG':<6} {'CHUNKS':>6}  {'UPLOADED'}")
    print("-" * 80)
    for r in rows:
        print(f"{r['source']:<40} {r['lang']:<6} {r['chunks']:>6}  {r['first_upload']}")


async def insert_docs(
    chunks: list[str],
    titles: list[str],
    embeddings: list[list[float]],
    lang: str,
    source: str,
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        for title, content, emb in zip(titles, chunks, embeddings):
            doc_id = await conn.fetchval(
                """
                INSERT INTO docs (source, title, content, lang)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                source, title, content, lang,
            )
            await conn.execute(
                """
                INSERT INTO doc_embeddings (doc_id, embedding)
                VALUES ($1, $2)
                """,
                doc_id, emb,
            )
            log.info("  inserted doc #%d  %s", doc_id, title[:60])


# ═══════════════════════════════════════════════════════════════════
# Main workflows
# ═══════════════════════════════════════════════════════════════════

async def ingest(pdf_path: str, lang: str, source: str, replace: bool):
    await init_db_pool()
    await ensure_schema()

    # ── optionally wipe old version ─────────────────────────────
    if replace:
        n = await delete_source(source)
        if n:
            log.info("🗑️  Deleted %d old chunks for source '%s'", n, source)

    # ── extract & chunk ─────────────────────────────────────────
    log.info("📄 Extracting text from %s ...", pdf_path)
    raw = extract_text_from_pdf(pdf_path)
    text = clean_text(raw)
    log.info("  extracted %d characters", len(text))

    log.info("✂️  Chunking ...")
    chunks = chunk_text(text)
    log.info("  created %d chunks", len(chunks))

    titles = [derive_title(c, i) for i, c in enumerate(chunks)]

    # ── embed ───────────────────────────────────────────────────
    log.info("🧠 Generating embeddings ...")
    embeddings = await embed_texts(chunks)

    # ── write ───────────────────────────────────────────────────
    log.info("💾 Writing to database (source=%s, lang=%s) ...", source, lang)
    await insert_docs(chunks, titles, embeddings, lang, source)
    await close_db_pool()

    log.info("✅ Done – %d chunks ingested for source '%s'.", len(chunks), source)


async def run_list_sources():
    await init_db_pool()
    await ensure_schema()
    await list_sources()
    await close_db_pool()


async def run_delete_source(source: str):
    await init_db_pool()
    await ensure_schema()
    n = await delete_source(source)
    if n:
        log.info("🗑️  Deleted %d chunks for source '%s'.", n, source)
    else:
        log.info("ℹ️  No chunks found for source '%s'.", source)
    await close_db_pool()


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage documentation in the RAG database.",
    )
    sub = parser.add_subparsers(dest="command")

    # ── upload ──────────────────────────────────────────────────
    upload = sub.add_parser("upload", help="Upload a PDF into the database.")
    upload.add_argument("pdf", help="Path to the PDF file")
    upload.add_argument(
        "--lang", default="en", choices=["en", "es", "pt"],
        help="Document language (default: en)",
    )
    upload.add_argument(
        "--source",
        help="Source name to track this upload (defaults to the PDF filename).",
    )
    upload.add_argument(
        "--append", action="store_true",
        help="Append chunks instead of replacing existing ones for this source.",
    )

    # ── list ────────────────────────────────────────────────────
    sub.add_parser("list", help="List all sources currently in the database.")

    # ── delete ──────────────────────────────────────────────────
    delete = sub.add_parser("delete", help="Delete all chunks for a source.")
    delete.add_argument("source", help="The source name to delete.")

    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "upload":
        pdf = Path(args.pdf)
        if not pdf.is_file():
            print(f"File not found: {pdf}", file=sys.stderr)
            sys.exit(1)
        source = args.source or pdf.name          # e.g. "wiki_en.pdf"
        replace = not args.append
        asyncio.run(ingest(str(pdf), args.lang, source, replace))

    elif args.command == "list":
        asyncio.run(run_list_sources())

    elif args.command == "delete":
        asyncio.run(run_delete_source(args.source))

    else:
        parser.print_help()
        sys.exit(1)

