"""Sync the GitHub wiki into the OpenAI support vector store.

Each wiki page becomes one Markdown knowledge file named
``wiki--<PageSlug>.md`` with front-matter that records the canonical wiki
URL. Sync state lives in vector store file attributes (``source``,
``wiki_slug``, ``content_sha256``), so the sync is stateless and only
touches files it uploaded itself.
"""

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

WIKI_KNOWLEDGE_FILENAME_PREFIX = "wiki--"
WIKI_KNOWLEDGE_SOURCE = "github-wiki"
DEFAULT_WIKI_BASE_URL = "https://github.com/DragonMineZ/dragonminez/wiki"

_EXCLUDED_PAGE_STEMS = {"_sidebar", "_footer"}
_WIKI_PAGE_SUFFIXES = {".md", ".markdown"}


@dataclass(frozen=True, slots=True)
class RenderedWikiPage:
    slug: str
    title: str
    filename: str
    content: str
    content_sha256: str
    source_url: str


@dataclass(frozen=True, slots=True)
class RemoteWikiFile:
    file_id: str
    wiki_slug: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class WikiSyncPlan:
    uploads: tuple[RenderedWikiPage, ...]
    deletes: tuple[RemoteWikiFile, ...]
    unchanged: int


def wiki_page_title(slug: str) -> str:
    return slug.replace("-", " ").strip() or slug


def wiki_page_url(slug: str, *, base_url: str = DEFAULT_WIKI_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/{quote(slug)}"


def wiki_knowledge_filename(slug: str) -> str:
    return f"{WIKI_KNOWLEDGE_FILENAME_PREFIX}{slug}.md"


def wiki_slug_from_filename(filename: str) -> str | None:
    name = (filename or "").strip()
    if not name.startswith(WIKI_KNOWLEDGE_FILENAME_PREFIX):
        return None
    slug = name[len(WIKI_KNOWLEDGE_FILENAME_PREFIX):]
    for suffix in _WIKI_PAGE_SUFFIXES:
        if slug.lower().endswith(suffix):
            slug = slug[: -len(suffix)]
            break
    return slug or None


def wiki_url_for_citation_filename(
    filename: str,
    *,
    base_url: str = DEFAULT_WIKI_BASE_URL,
) -> tuple[str, str] | None:
    """Map a cited knowledge filename to ``(title, url)``; None if not a wiki file."""
    slug = wiki_slug_from_filename(filename)
    if slug is None:
        return None
    return wiki_page_title(slug), wiki_page_url(slug, base_url=base_url)


def render_wiki_knowledge_file(
    *,
    slug: str,
    body: str,
    base_url: str = DEFAULT_WIKI_BASE_URL,
) -> RenderedWikiPage:
    title = wiki_page_title(slug)
    source_url = wiki_page_url(slug, base_url=base_url)
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    content = (
        f"# {title}\n"
        "\n"
        f"Source: {source_url}\n"
        "Origin: DragonMineZ GitHub wiki (synced automatically; edit the wiki, not this file)\n"
        "\n"
        f"{normalized_body}\n"
    )
    return RenderedWikiPage(
        slug=slug,
        title=title,
        filename=wiki_knowledge_filename(slug),
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        source_url=source_url,
    )


def load_wiki_pages(
    wiki_dir: Path,
    *,
    base_url: str = DEFAULT_WIKI_BASE_URL,
) -> list[RenderedWikiPage]:
    pages: list[RenderedWikiPage] = []
    for path in sorted(wiki_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _WIKI_PAGE_SUFFIXES:
            continue
        if path.stem.lower() in _EXCLUDED_PAGE_STEMS:
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        if not body.strip():
            continue
        pages.append(render_wiki_knowledge_file(slug=path.stem, body=body, base_url=base_url))
    return pages


def plan_wiki_sync(
    pages: list[RenderedWikiPage],
    remote_files: list[RemoteWikiFile],
) -> WikiSyncPlan:
    remote_by_slug: dict[str, RemoteWikiFile] = {entry.wiki_slug: entry for entry in remote_files}
    local_slugs = {page.slug for page in pages}

    uploads: list[RenderedWikiPage] = []
    deletes: list[RemoteWikiFile] = []
    unchanged = 0

    for page in pages:
        remote = remote_by_slug.get(page.slug)
        if remote is not None and remote.content_sha256 == page.content_sha256:
            unchanged += 1
            continue
        uploads.append(page)
        if remote is not None:
            deletes.append(remote)

    for remote in remote_files:
        if remote.wiki_slug not in local_slugs:
            deletes.append(remote)

    return WikiSyncPlan(uploads=tuple(uploads), deletes=tuple(deletes), unchanged=unchanged)


def _remote_file_from_attributes(file_id: str, attributes: Any) -> RemoteWikiFile | None:
    if not isinstance(attributes, dict):
        return None
    if attributes.get("source") != WIKI_KNOWLEDGE_SOURCE:
        return None
    slug = str(attributes.get("wiki_slug") or "").strip()
    if not slug:
        return None
    return RemoteWikiFile(
        file_id=file_id,
        wiki_slug=slug,
        content_sha256=str(attributes.get("content_sha256") or ""),
    )


async def list_remote_wiki_files(
    *,
    vector_store_id: str,
    openai_client: Any | None = None,
) -> list[RemoteWikiFile]:
    """List wiki-managed files in the vector store; unmanaged files are ignored."""
    resolved_client = openai_client
    if resolved_client is None:
        from bulmaai.services.openai_client import client as resolved_client

    remote_files: list[RemoteWikiFile] = []
    async for entry in resolved_client.vector_stores.files.list(
        vector_store_id=vector_store_id,
        limit=100,
    ):
        remote = _remote_file_from_attributes(
            str(getattr(entry, "id")),
            getattr(entry, "attributes", None),
        )
        if remote is not None:
            remote_files.append(remote)
    return remote_files


async def apply_wiki_sync(
    plan: WikiSyncPlan,
    *,
    vector_store_id: str,
    openai_client: Any | None = None,
) -> dict[str, int]:
    resolved_client = openai_client
    if resolved_client is None:
        from bulmaai.services.openai_client import client as resolved_client

    uploaded = 0
    for page in plan.uploads:
        created_file = await resolved_client.files.create(
            file=(page.filename, page.content.encode("utf-8")),
            purpose="assistants",
        )
        file_id = str(getattr(created_file, "id"))
        await resolved_client.vector_stores.files.create_and_poll(
            vector_store_id=vector_store_id,
            file_id=file_id,
            attributes={
                "source": WIKI_KNOWLEDGE_SOURCE,
                "wiki_slug": page.slug,
                "content_sha256": page.content_sha256,
                "source_url": page.source_url,
            },
        )
        uploaded += 1
        log.info("Uploaded wiki knowledge file %s", page.filename)

    deleted = 0
    for remote in plan.deletes:
        await resolved_client.vector_stores.files.delete(
            remote.file_id,
            vector_store_id=vector_store_id,
        )
        try:
            await resolved_client.files.delete(remote.file_id)
        except Exception:
            log.warning("Detached but could not delete file %s", remote.file_id, exc_info=True)
        deleted += 1
        log.info("Removed stale wiki knowledge file for slug %s", remote.wiki_slug)

    return {"uploaded": uploaded, "deleted": deleted, "unchanged": plan.unchanged}
