"""Sync the DragonMineZ wiki (MediaWiki) into the support knowledge store (``knowledge_docs``).

Each content page, read through the MediaWiki API, becomes one Markdown
knowledge doc per ``==`` section: the intro is ``wiki--<Page_Title>.md``, each
section ``wiki--<Page_Title>--<section_slug>.md``, with a header that records the
section URL. Sections too long for knowledge.search's excerpt are split again on
``===`` headings, then on paragraphs. Translation pages and the main page are
skipped. The sync compares content hashes against the ``mediawiki`` rows and only
touches those; cogs/knowledge_sync.py runs it every 6 hours.
"""

import asyncio
import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from bulmaai.services import knowledge

log = logging.getLogger(__name__)

WIKI_KNOWLEDGE_FILENAME_PREFIX = "wiki--"
WIKI_KNOWLEDGE_SOURCE = "mediawiki"
DEFAULT_WIKI_BASE_URL = "https://wiki.dragonminez.com/wiki/Special:MyLanguage"
MEDIAWIKI_USER_AGENT = "BulmaAI wiki knowledge sync (+https://github.com/DragonMineZ/dragonminez-ai)"
MEDIAWIKI_TIMEOUT_SECONDS = 30
MEDIAWIKI_TITLES_PER_REQUEST = 50
# Whole rendered doc, header included: knowledge.search only shows the first SEARCH_TEXT_CHARS (1500) of a hit.
WIKI_CHUNK_MAX_CHARS = 1400
_MIN_CHUNK_BODY_CHARS = 200

_TRANSLATION_PAGE_PROP = "translate-is-translation"
_WIKI_PAGE_SUFFIXES = {".md", ".markdown"}

_TVAR_RE = re.compile(r"<tvar\s+name\s*=[^>]*>(.*?)</tvar>", re.IGNORECASE | re.DOTALL)
_LEGACY_TVAR_RE = re.compile(r"<tvar\|[^>]*>(.*?)</>", re.IGNORECASE | re.DOTALL)
_TRANSLATE_TAG_RE = re.compile(r"</?translate(?:\s[^>]*)?>", re.IGNORECASE)
_TRANSLATION_UNIT_MARKER_RE = re.compile(r"<!--T:\d+-->[ \t]*")
_LANGUAGES_TAG_RE = re.compile(r"<languages\s*/>", re.IGNORECASE)
_BEHAVIOR_SWITCH_RE = re.compile(
    r"__(?:NOTOC|FORCETOC|TOC|NOEDITSECTION|NEWSECTIONLINK|NONEWSECTIONLINK|NOGALLERY|HIDDENCAT"
    r"|EXPECTUNUSEDCATEGORY|EXPECTUNUSEDTEMPLATE|NOCONTENTCONVERT|NOCC|NOTITLECONVERT|NOTC"
    r"|INDEX|NOINDEX|STATICREDIRECT|DISAMBIG)__"
)
_MY_LANGUAGE_LINK_RE = re.compile(r"(\[\[\s*):?\s*Special:MyLanguage/", re.IGNORECASE)
_TRAILING_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_EXTRA_BLANK_LINES_RE = re.compile(r"\n{3,}")
_H2_RE = re.compile(r"^==(?!=)[ \t]*(.+?)[ \t]*==[ \t]*$", re.MULTILINE)
_H3_RE = re.compile(r"^===(?!=)[ \t]*(.+?)[ \t]*===[ \t]*$", re.MULTILINE)
_HEADING_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
_HEADING_MARKUP_RE = re.compile(r"'{2,}|<[^>]+>")
_SECTION_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PAGE_DASH_RE = re.compile(r"-(?=-|$)")


@dataclass(frozen=True, slots=True)
class RenderedWikiPage:
    slug: str
    title: str
    filename: str
    content: str
    content_sha256: str
    source_url: str


@dataclass(frozen=True, slots=True)
class WikiSyncPlan:
    uploads: tuple[RenderedWikiPage, ...]
    deletes: tuple[str, ...]  # knowledge_docs filenames
    unchanged: int


def wiki_page_slug(title: str) -> str:
    return title.strip().replace(" ", "_")


def wiki_page_title(slug: str) -> str:
    return slug.replace("_", " ").strip() or slug


def wiki_page_url(slug: str, *, base_url: str = DEFAULT_WIKI_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/{quote(slug)}"


def wiki_knowledge_filename(slug: str, section: str = "") -> str:
    # Subpage titles contain "/"; encode it so the filename stays a plain file name (data/knowledge, logs).
    # A doubled or trailing "-" is encoded too, so the first "--" always starts the section part.
    page = _PAGE_DASH_RE.sub("%2D", slug.replace("/", "%2F"))
    return f"{WIKI_KNOWLEDGE_FILENAME_PREFIX}{page}{f'--{section}' if section else ''}.md"


def wiki_slug_from_filename(filename: str) -> str | None:
    name = (filename or "").strip()
    if not name.startswith(WIKI_KNOWLEDGE_FILENAME_PREFIX):
        return None
    stem = name[len(WIKI_KNOWLEDGE_FILENAME_PREFIX):]
    for suffix in _WIKI_PAGE_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    # Section docs cite their page.
    return stem.split("--", 1)[0].replace("%2D", "-").replace("%2F", "/") or None


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


def _chunk_content(title: str, source_url: str, body: str) -> str:
    return (
        f"# {title}\n"
        "\n"
        f"Source: {source_url}\n"
        "Origin: DragonMineZ wiki at wiki.dragonminez.com (synced automatically; edit the wiki, not this file)\n"
        "\n"
        f"{body}\n"
    )


def _heading_text(raw: str) -> str:
    text = _HEADING_MARKUP_RE.sub("", _HEADING_LINK_RE.sub(r"\1", raw))
    return " ".join(text.split()) or raw.strip()


def _section_slug(name: str) -> str:
    return _SECTION_SLUG_RE.sub("_", name.lower()).strip("_") or "section"


def _unique(name: str, used: set[str], sep: str) -> str:
    """MediaWiki-style de-duplication: Foo, Foo_2, Foo_3."""
    candidate, count = name, 1
    while candidate in used:
        count += 1
        candidate = f"{name}{sep}{count}"
    used.add(candidate)
    return candidate


def _split_headings(text: str, pattern: re.Pattern[str]) -> tuple[str, list[tuple[str, str]]]:
    """(text before the first heading, [(heading, body)])."""
    matches = list(pattern.finditer(text))
    ends = [match.start() for match in matches[1:]] + [len(text)]
    lead = text[: matches[0].start()] if matches else text
    return lead.strip(), [(_heading_text(m.group(1)), text[m.end():end].strip()) for m, end in zip(matches, ends)]


def _pack(text: str, budget: int) -> list[str]:
    """Greedily pack paragraphs into parts of at most budget chars; oversized paragraphs split by line, then hard."""
    pieces: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        if len(block) <= budget:
            pieces.append(("\n\n", block))
            continue
        for line in block.split("\n"):
            pieces.extend(("\n", line[i:i + budget]) for i in range(0, len(line), budget))
    parts: list[str] = []
    current = ""
    for sep, piece in pieces:
        if current and len(current) + len(sep) + len(piece) <= budget:
            current += sep + piece
            continue
        if current:
            parts.append(current)
        current = piece
    if current:
        parts.append(current)
    return parts


def render_wiki_page_chunks(
    *,
    slug: str,
    body: str,
    base_url: str = DEFAULT_WIKI_BASE_URL,
) -> list[RenderedWikiPage]:
    """One doc for the intro and one per == section, each at most WIKI_CHUNK_MAX_CHARS."""
    page_title = wiki_page_title(slug)
    page_url = wiki_page_url(slug, base_url=base_url)
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    intro, sections = _split_headings(normalized_body, _H2_RE)

    pieces: list[tuple[str, str, str, str]] = [(page_title, page_url, "", intro)]  # (title, url, file key, text)
    anchors: set[str] = set()
    for name, text in sections:
        title = f"{page_title}: {name}"
        url = f"{page_url}#{quote(_unique(name.replace(' ', '_'), anchors, '_'))}"
        key = _section_slug(name)
        lead, subsections = _split_headings(text, _H3_RE)
        sub_anchors = [_unique(sub_name.replace(" ", "_"), anchors, "_") for sub_name, _ in subsections]
        if not subsections or len(_chunk_content(title, url, text)) <= WIKI_CHUNK_MAX_CHARS:
            pieces.append((title, url, key, text))
            continue
        pieces.append((title, url, key, lead))
        for (sub_name, sub_text), sub_anchor in zip(subsections, sub_anchors):
            pieces.append(
                (f"{title} / {sub_name}", f"{page_url}#{quote(sub_anchor)}", f"{key}_{_section_slug(sub_name)}", sub_text)
            )

    chunks: list[RenderedWikiPage] = []
    stems: set[str] = set()
    for title, url, key, text in pieces:
        if not text:
            continue
        budget = max(WIKI_CHUNK_MAX_CHARS - len(_chunk_content(f"{title} (99/99)", url, "")), _MIN_CHUNK_BODY_CHARS)
        parts = _pack(text, budget)
        for index, part in enumerate(parts):
            part_title = title if len(parts) == 1 else f"{title} ({index + 1}/{len(parts)})"
            stem = key if index == 0 else f"{key or 'intro'}-{index + 1}"
            content = _chunk_content(part_title, url, part)
            chunks.append(
                RenderedWikiPage(
                    slug=slug,
                    title=part_title,
                    filename=wiki_knowledge_filename(slug, _unique(stem, stems, "-")),
                    content=content,
                    content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    source_url=url,
                )
            )
    return chunks


def clean_wikitext(text: str) -> str:
    """Strip Translate markup and behaviour switches; templates and tables stay as-is."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _TVAR_RE.sub(r"\1", cleaned)
    cleaned = _LEGACY_TVAR_RE.sub(r"\1", cleaned)
    cleaned = _TRANSLATE_TAG_RE.sub("", cleaned)
    cleaned = _TRANSLATION_UNIT_MARKER_RE.sub("", cleaned)
    cleaned = _LANGUAGES_TAG_RE.sub("", cleaned)
    cleaned = _BEHAVIOR_SWITCH_RE.sub("", cleaned)
    cleaned = _MY_LANGUAGE_LINK_RE.sub(r"\1", cleaned)
    cleaned = _TRAILING_SPACE_RE.sub("", cleaned)
    return _EXTRA_BLANK_LINES_RE.sub("\n\n", cleaned).strip()


def _mediawiki_get(session: Any, api_url: str, params: dict[str, str]) -> dict[str, Any]:
    response = session.get(
        api_url,
        params={**params, "format": "json", "formatversion": "2"},
        headers={"User-Agent": MEDIAWIKI_USER_AGENT},
        timeout=MEDIAWIKI_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected MediaWiki API response from {api_url}.")
    error = data.get("error")
    if error:
        raise RuntimeError(f"MediaWiki API error: {error.get('code')}: {error.get('info')}")
    return data


def _mediawiki_query(session: Any, api_url: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
    continuation: dict[str, str] = {}
    while True:
        data = _mediawiki_get(session, api_url, {"action": "query", **params, **continuation})
        yield data.get("query") or {}
        continuation = data.get("continue") or {}
        if not continuation:
            return


def _is_translation_page(page: dict[str, Any], content_language: str) -> bool:
    if _TRANSLATION_PAGE_PROP in (page.get("pageprops") or {}):
        return True
    page_language = str(page.get("pagelanguage") or "")
    return bool(content_language and page_language and page_language != content_language)


def _page_wikitext(page: dict[str, Any]) -> str:
    revisions = page.get("revisions") or []
    if not revisions:
        return ""
    return str(((revisions[0].get("slots") or {}).get("main") or {}).get("content") or "")


def load_mediawiki_pages(
    api_url: str,
    *,
    base_url: str = DEFAULT_WIKI_BASE_URL,
    session: requests.Session | None = None,
) -> list[RenderedWikiPage]:
    """Render every main-namespace content page of the wiki as section-sized knowledge docs.

    Skips redirects, the main page, Translate translation pages (including the
    source-language copy) and pages that are empty once cleaned.
    """
    http = session if session is not None else requests.Session()
    try:
        general = _mediawiki_get(
            http, api_url, {"action": "query", "meta": "siteinfo", "siprop": "general"}
        ).get("query", {}).get("general", {})
        content_language = str(general.get("lang") or "")
        main_page = str(general.get("mainpage") or "")

        titles: list[str] = []
        for query in _mediawiki_query(
            http,
            api_url,
            {"list": "allpages", "apnamespace": "0", "apfilterredir": "nonredirects", "aplimit": "max"},
        ):
            titles.extend(str(entry["title"]) for entry in query.get("allpages") or [])
        titles = [title for title in titles if title != main_page]

        pages: list[RenderedWikiPage] = []
        for start in range(0, len(titles), MEDIAWIKI_TITLES_PER_REQUEST):
            batch = titles[start:start + MEDIAWIKI_TITLES_PER_REQUEST]
            details: dict[str, dict[str, Any]] = {}
            for query in _mediawiki_query(
                http,
                api_url,
                {
                    "prop": "revisions|info|pageprops",
                    "titles": "|".join(batch),
                    "rvprop": "content",
                    "rvslots": "main",
                    "ppprop": _TRANSLATION_PAGE_PROP,
                },
            ):
                for entry in query.get("pages") or []:
                    details.setdefault(str(entry.get("title")), {}).update(entry)

            for title in batch:
                page = details.get(title)
                if not page or page.get("missing") or page.get("invalid"):
                    continue
                if _is_translation_page(page, content_language):
                    continue
                body = clean_wikitext(_page_wikitext(page))
                if not body:
                    continue
                pages.extend(render_wiki_page_chunks(slug=wiki_page_slug(title), body=body, base_url=base_url))
    finally:
        if session is None:
            http.close()

    log.info("Loaded %d wiki section docs from %s (%d candidate titles)", len(pages), api_url, len(titles))
    return pages


def plan_wiki_sync(pages: list[RenderedWikiPage], stored: dict[str, str]) -> WikiSyncPlan:
    """stored: {filename: content_sha256} of the mediawiki rows in knowledge_docs."""
    remaining = dict(stored)
    uploads: list[RenderedWikiPage] = []
    unchanged = 0
    for page in pages:
        if remaining.pop(page.filename, None) == page.content_sha256:
            unchanged += 1
        else:
            uploads.append(page)
    return WikiSyncPlan(uploads=tuple(uploads), deletes=tuple(sorted(remaining)), unchanged=unchanged)


async def apply_wiki_sync(plan: WikiSyncPlan, *, pool: Any | None = None) -> dict[str, int]:
    for page in plan.uploads:
        await knowledge.upsert(
            page.filename,
            source=WIKI_KNOWLEDGE_SOURCE,
            title=page.title,
            content=page.content,
            url=page.source_url,
            pool=pool,
        )
    for filename in plan.deletes:
        await knowledge.delete(filename, pool=pool)
    return {"uploaded": len(plan.uploads), "deleted": len(plan.deletes), "unchanged": plan.unchanged}


async def sync_wiki_knowledge(
    api_url: str,
    *,
    base_url: str = DEFAULT_WIKI_BASE_URL,
    pool: Any | None = None,
) -> dict[str, int]:
    """Mirror the wiki into knowledge_docs: upsert new/changed pages, delete pages gone from the wiki."""
    pages = await asyncio.to_thread(load_mediawiki_pages, api_url, base_url=base_url)
    if not pages:
        # An empty wiki is an outage or a bad URL, not a real state; syncing it would wipe the knowledge.
        raise RuntimeError(f"No wiki pages found at {api_url}; refusing to sync an empty set.")
    stored = await knowledge.list_by_source(WIKI_KNOWLEDGE_SOURCE, pool=pool)
    return await apply_wiki_sync(plan_wiki_sync(pages, stored), pool=pool)
