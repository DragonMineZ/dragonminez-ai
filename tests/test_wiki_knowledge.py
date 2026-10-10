import os
import unittest
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.wiki_knowledge import (
    MEDIAWIKI_TITLES_PER_REQUEST,
    MEDIAWIKI_USER_AGENT,
    RemoteWikiFile,
    clean_wikitext,
    list_remote_wiki_files,
    load_mediawiki_pages,
    plan_wiki_sync,
    render_wiki_knowledge_file,
    wiki_knowledge_filename,
    wiki_page_slug,
    wiki_page_title,
    wiki_page_url,
    wiki_slug_from_filename,
    wiki_url_for_citation_filename,
)

WIKI_BASE_URL = "https://wiki.dragonminez.com/wiki/Special:MyLanguage"
API_URL = "https://wiki.dragonminez.com/api.php"


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeMediaWiki:
    """A tiny api.php: siteinfo, paged allpages and revision continuation after the first page of a batch."""

    def __init__(
        self,
        pages: dict[str, dict[str, Any]],
        *,
        main_page: str = "Main Page",
        allpages_chunk: int = 2,
    ) -> None:
        self.pages = pages
        self.main_page = main_page
        self.allpages_chunk = allpages_chunk
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers), "timeout": timeout})
        if params.get("meta") == "siteinfo":
            return _FakeResponse({"query": {"general": {"mainpage": self.main_page, "lang": "en"}}})
        if params.get("list") == "allpages":
            return _FakeResponse(self._allpages(params))
        return _FakeResponse(self._page_details(params))

    def _allpages(self, params: dict[str, str]) -> dict[str, Any]:
        titles = sorted(self.pages)
        start = titles.index(params["apcontinue"]) if "apcontinue" in params else 0
        chunk = titles[start:start + self.allpages_chunk]
        payload: dict[str, Any] = {"query": {"allpages": [{"ns": 0, "title": title} for title in chunk]}}
        if start + self.allpages_chunk < len(titles):
            payload["continue"] = {"apcontinue": titles[start + self.allpages_chunk], "continue": "-||"}
        return payload

    def _page_details(self, params: dict[str, str]) -> dict[str, Any]:
        titles = params["titles"].split("|")
        continuing = "rvcontinue" in params
        entries = []
        for index, title in enumerate(titles):
            page = self.pages[title]
            entry: dict[str, Any] = {"ns": 0, "title": title}
            if not continuing:
                entry["pagelanguage"] = page.get("lang", "en")
                if page.get("props"):
                    entry["pageprops"] = page["props"]
            if (index == 0) != continuing:
                entry["revisions"] = [{"slots": {"main": {"content": page.get("content", "")}}}]
            entries.append(entry)
        payload: dict[str, Any] = {"query": {"pages": entries}}
        if not continuing and len(titles) > 1:
            payload["continue"] = {"rvcontinue": "next", "continue": "||info|pageprops"}
        return payload


class WikiKnowledgeHelperTests(unittest.TestCase):
    def test_slug_title_and_url_from_page_title(self) -> None:
        self.assertEqual(wiki_page_slug("Frost Demon"), "Frost_Demon")
        self.assertEqual(wiki_page_slug("Bio-Android"), "Bio-Android")
        self.assertEqual(wiki_page_slug("Beginner's Guide"), "Beginner's_Guide")
        self.assertEqual(wiki_page_title("Frost_Demon"), "Frost Demon")
        self.assertEqual(wiki_page_title("Bio-Android"), "Bio-Android")
        self.assertEqual(wiki_page_url("Frost_Demon"), f"{WIKI_BASE_URL}/Frost_Demon")
        self.assertEqual(wiki_page_url("Beginner's_Guide"), f"{WIKI_BASE_URL}/Beginner%27s_Guide")
        self.assertEqual(
            wiki_page_url("Raids", base_url="https://wiki.example.test/wiki/"),
            "https://wiki.example.test/wiki/Raids",
        )

    def test_filename_roundtrip(self) -> None:
        filename = wiki_knowledge_filename("Custom_Races")
        self.assertEqual(filename, "wiki--Custom_Races.md")
        self.assertEqual(wiki_slug_from_filename(filename), "Custom_Races")

    def test_slug_from_unmanaged_filename_is_none(self) -> None:
        self.assertIsNone(wiki_slug_from_filename("patreon-discord-role-link.md"))
        self.assertIsNone(wiki_slug_from_filename("Dragonmine_Z_Trello_ExportedScript.pdf"))
        self.assertIsNone(wiki_slug_from_filename(""))

    def test_citation_filename_maps_to_title_and_url(self) -> None:
        self.assertEqual(
            wiki_url_for_citation_filename("wiki--Dragons_and_Wishes.md"),
            ("Dragons and Wishes", f"{WIKI_BASE_URL}/Dragons_and_Wishes"),
        )
        self.assertEqual(
            wiki_url_for_citation_filename("wiki--Bio-Android.md"),
            ("Bio-Android", f"{WIKI_BASE_URL}/Bio-Android"),
        )
        self.assertIsNone(wiki_url_for_citation_filename("dragonminez-faq.md"))


class CleanWikitextTests(unittest.TestCase):
    def test_strips_translate_markup_and_keeps_templates_and_tables(self) -> None:
        source = (
            "__NOTOC__\r\n"
            "<languages/>\n"
            "<translate>\n"
            "<!--T:1-->\n"
            "'''Janemba''' is a world boss.\n"
            "\n"
            "== Finding the lair == <!--T:2-->\n"
            "\n"
            "<!--T:3-->\n"
            "* Use <tvar name=\"cmd\"><code>/dmzworldboss locate janemba</code></tvar>.\n"
            "* See [[Special:MyLanguage/World Bosses|World Bosses]] and [[special:MyLanguage/Gomah]].\n"
            "</translate>\n"
            "{{World Boss Infobox\n"
            "|quote=<translate><!--T:4--> The demon.</translate>\n"
            "}}\n"
            "{| class=\"wikitable\"\n"
            "! A !! B\n"
            "|}\n"
            "\n"
            "\n"
            "\n"
            "<languages />\n"
            "__NOEDITSECTION__ __TOC__\n"
        )

        self.assertEqual(
            clean_wikitext(source),
            "'''Janemba''' is a world boss.\n"
            "\n"
            "== Finding the lair ==\n"
            "\n"
            "* Use <code>/dmzworldboss locate janemba</code>.\n"
            "* See [[World Bosses|World Bosses]] and [[Gomah]].\n"
            "\n"
            "{{World Boss Infobox\n"
            "|quote=The demon.\n"
            "}}\n"
            "{| class=\"wikitable\"\n"
            "! A !! B\n"
            "|}",
        )

    def test_markup_only_page_cleans_to_empty(self) -> None:
        self.assertEqual(clean_wikitext("<languages />\n__NOTOC__\n<translate>\n</translate>\n"), "")


class LoadMediaWikiPagesTests(unittest.TestCase):
    def _wiki(self) -> _FakeMediaWiki:
        return _FakeMediaWiki(
            {
                "Main Page": {"content": "Welcome hub"},
                "Main Page/en": {"content": "Welcome hub", "props": {"translate-is-translation": ""}},
                "Bio-Android": {"content": "<translate><!--T:1--> Absorbs others.</translate>"},
                "Beginner's Guide": {"content": "Start here."},
                "Empty Page": {"content": "  <languages/>\n"},
                "Frost Demon": {"content": "See [[Special:MyLanguage/Races|races]]."},
                "Frost Demon/en": {"content": "See races.", "props": {"translate-is-translation": ""}},
                "Frost Demon/es": {"content": "Mira las razas.", "lang": "es"},
                "Saiyan Pt": {"content": "Página em português.", "lang": "pt"},
            }
        )

    def test_loads_source_pages_and_skips_main_translation_and_empty_pages(self) -> None:
        wiki = self._wiki()

        pages = load_mediawiki_pages(API_URL, base_url=WIKI_BASE_URL, session=wiki)

        self.assertEqual(
            [page.slug for page in pages],
            ["Beginner's_Guide", "Bio-Android", "Frost_Demon"],
        )
        by_slug = {page.slug: page for page in pages}
        self.assertEqual(by_slug["Bio-Android"].title, "Bio-Android")
        self.assertEqual(by_slug["Bio-Android"].source_url, f"{WIKI_BASE_URL}/Bio-Android")
        self.assertIn("Absorbs others.", by_slug["Bio-Android"].content)
        self.assertNotIn("<translate>", by_slug["Bio-Android"].content)
        self.assertNotIn("<!--T:", by_slug["Bio-Android"].content)
        self.assertIn("See [[Races|races]].", by_slug["Frost_Demon"].content)
        self.assertEqual(by_slug["Beginner's_Guide"].source_url, f"{WIKI_BASE_URL}/Beginner%27s_Guide")

    def test_follows_allpages_continuation_and_sends_expected_params(self) -> None:
        wiki = self._wiki()

        load_mediawiki_pages(API_URL, base_url=WIKI_BASE_URL, session=wiki)

        allpages_calls = [call for call in wiki.calls if call["params"].get("list") == "allpages"]
        self.assertEqual(len(allpages_calls), 5)
        for call in allpages_calls:
            self.assertEqual(call["params"]["apnamespace"], "0")
            self.assertEqual(call["params"]["apfilterredir"], "nonredirects")
        detail_calls = [call for call in wiki.calls if "titles" in call["params"]]
        self.assertTrue(detail_calls)
        for call in detail_calls:
            self.assertNotIn("Main Page", call["params"]["titles"].split("|"))
            self.assertEqual(call["params"]["rvprop"], "content")
            self.assertEqual(call["params"]["rvslots"], "main")
            self.assertIn("revisions", call["params"]["prop"].split("|"))
            self.assertIn("info", call["params"]["prop"].split("|"))
        for call in wiki.calls:
            self.assertEqual(call["url"], API_URL)
            self.assertEqual(call["headers"]["User-Agent"], MEDIAWIKI_USER_AGENT)
            self.assertEqual(call["params"]["format"], "json")
            self.assertEqual(call["params"]["formatversion"], "2")

    def test_requests_at_most_fifty_titles_per_batch(self) -> None:
        wiki = _FakeMediaWiki(
            {f"Page {index:03d}": {"content": f"Body {index}"} for index in range(MEDIAWIKI_TITLES_PER_REQUEST + 5)},
            allpages_chunk=500,
        )

        pages = load_mediawiki_pages(API_URL, base_url=WIKI_BASE_URL, session=wiki)

        self.assertEqual(len(pages), MEDIAWIKI_TITLES_PER_REQUEST + 5)
        batch_sizes = [
            len(call["params"]["titles"].split("|"))
            for call in wiki.calls
            if "titles" in call["params"] and "rvcontinue" not in call["params"]
        ]
        self.assertEqual(batch_sizes, [MEDIAWIKI_TITLES_PER_REQUEST, 5])

    def test_api_error_raises(self) -> None:
        class ErrorSession:
            def get(self, url: str, **kwargs: Any) -> _FakeResponse:
                return _FakeResponse({"error": {"code": "readapidenied", "info": "You need read permission."}})

        with self.assertRaisesRegex(RuntimeError, "readapidenied"):
            load_mediawiki_pages(API_URL, base_url=WIKI_BASE_URL, session=ErrorSession())


class WikiKnowledgeRenderTests(unittest.TestCase):
    def test_render_includes_title_source_and_body(self) -> None:
        page = render_wiki_knowledge_file(slug="Custom_Forms", body="Use JSON.\r\nReload after.")

        self.assertEqual(page.filename, "wiki--Custom_Forms.md")
        self.assertIn("# Custom Forms", page.content)
        self.assertIn(f"Source: {WIKI_BASE_URL}/Custom_Forms", page.content)
        self.assertIn("Origin: DragonMineZ wiki at wiki.dragonminez.com", page.content)
        self.assertIn("Use JSON.\nReload after.", page.content)
        self.assertEqual(len(page.content_sha256), 64)

    def test_render_hash_changes_with_body(self) -> None:
        first = render_wiki_knowledge_file(slug="Raids", body="v1")
        second = render_wiki_knowledge_file(slug="Raids", body="v2")

        self.assertNotEqual(first.content_sha256, second.content_sha256)


class PlanWikiSyncTests(unittest.TestCase):
    def test_plan_uploads_new_and_changed_and_deletes_stale(self) -> None:
        unchanged = render_wiki_knowledge_file(slug="Raids", body="same")
        changed = render_wiki_knowledge_file(slug="Sagas", body="new content")
        new = render_wiki_knowledge_file(slug="Dragons_and_Wishes", body="fresh")

        remote_unchanged = RemoteWikiFile(
            file_id="file-1", wiki_slug="Raids", content_sha256=unchanged.content_sha256
        )
        remote_changed = RemoteWikiFile(
            file_id="file-2", wiki_slug="Sagas", content_sha256="old-hash"
        )
        remote_stale = RemoteWikiFile(
            file_id="file-3", wiki_slug="Deleted_Page", content_sha256="whatever"
        )

        plan = plan_wiki_sync(
            [unchanged, changed, new],
            [remote_unchanged, remote_changed, remote_stale],
        )

        self.assertEqual([page.slug for page in plan.uploads], ["Sagas", "Dragons_and_Wishes"])
        self.assertEqual(
            sorted(remote.file_id for remote in plan.deletes),
            ["file-2", "file-3"],
        )
        self.assertEqual(plan.unchanged, 1)

    def test_plan_with_empty_remote_uploads_everything(self) -> None:
        pages = [render_wiki_knowledge_file(slug="Races", body="hub")]

        plan = plan_wiki_sync(pages, [])

        self.assertEqual(len(plan.uploads), 1)
        self.assertEqual(plan.deletes, ())
        self.assertEqual(plan.unchanged, 0)

    def test_plan_keeps_one_current_copy_and_deletes_duplicates(self) -> None:
        page = render_wiki_knowledge_file(slug="Raids", body="same")

        plan = plan_wiki_sync(
            [page],
            [
                RemoteWikiFile(file_id="file-legacy", wiki_slug="Raids", content_sha256="github-hash"),
                RemoteWikiFile(file_id="file-current", wiki_slug="Raids", content_sha256=page.content_sha256),
            ],
        )

        self.assertEqual(plan.uploads, ())
        self.assertEqual([remote.file_id for remote in plan.deletes], ["file-legacy"])
        self.assertEqual(plan.unchanged, 1)


class _FakeVectorStoreFiles:
    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self.entries = entries
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any):
        self.list_calls.append(kwargs)
        entries = self.entries

        async def iterate():
            for entry in entries:
                yield entry

        return iterate()


class ListRemoteWikiFilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_github_wiki_files_are_managed_and_planned_for_deletion(self) -> None:
        files = _FakeVectorStoreFiles(
            [
                SimpleNamespace(
                    id="file-legacy",
                    attributes={"source": "github-wiki", "wiki_slug": "Stats-and-Attributes", "content_sha256": "a"},
                ),
                SimpleNamespace(
                    id="file-legacy-same-slug",
                    attributes={"source": "github-wiki", "wiki_slug": "Raids", "content_sha256": "b"},
                ),
                SimpleNamespace(
                    id="file-removed-page",
                    attributes={"source": "mediawiki", "wiki_slug": "Saiyan", "content_sha256": "c"},
                ),
                SimpleNamespace(
                    id="file-faq",
                    attributes={"source": "support-faq", "wiki_slug": "Raids", "content_sha256": "d"},
                ),
                SimpleNamespace(id="file-ticket", attributes=None),
            ]
        )
        client = SimpleNamespace(vector_stores=SimpleNamespace(files=files))

        remote = await list_remote_wiki_files(vector_store_id="vs_wiki", openai_client=client)

        self.assertEqual(
            [entry.file_id for entry in remote],
            ["file-legacy", "file-legacy-same-slug", "file-removed-page"],
        )
        self.assertEqual(files.list_calls[0]["vector_store_id"], "vs_wiki")

        pages = [
            render_wiki_knowledge_file(slug="Stats_and_Attributes", body="stats"),
            render_wiki_knowledge_file(slug="Raids", body="raids"),
        ]
        plan = plan_wiki_sync(pages, remote)

        self.assertEqual([page.slug for page in plan.uploads], ["Stats_and_Attributes", "Raids"])
        self.assertEqual(
            sorted(entry.file_id for entry in plan.deletes),
            ["file-legacy", "file-legacy-same-slug", "file-removed-page"],
        )


class WikiCitationExtractionTests(unittest.TestCase):
    def _response_with_annotations(self, annotations: list[SimpleNamespace]) -> SimpleNamespace:
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="output_text",
                            text="answer",
                            annotations=annotations,
                        )
                    ],
                )
            ]
        )

    def test_extracts_cited_filenames_in_order_without_duplicates(self) -> None:
        from bulmaai.services.openai_client import _extract_file_citations

        response = self._response_with_annotations(
            [
                SimpleNamespace(type="file_citation", filename="wiki--Raids.md"),
                SimpleNamespace(type="file_citation", filename="wiki--Raids.md"),
                SimpleNamespace(type="file_citation", filename="dragonminez-faq.md"),
                SimpleNamespace(type="url_citation", filename="ignored.md"),
            ]
        )

        self.assertEqual(
            _extract_file_citations(response),
            ["wiki--Raids.md", "dragonminez-faq.md"],
        )

    def test_append_wiki_sources_links_only_wiki_files(self) -> None:
        from bulmaai.services.openai_client import _append_wiki_sources

        reply = _append_wiki_sources(
            "Do the raid at night.",
            ["wiki--World_Bosses.md", "wiki--Bio-Android.md", "dragonminez-faq.md"],
            base_url=WIKI_BASE_URL,
        )

        self.assertIn("Do the raid at night.", reply)
        self.assertIn(f"[World Bosses](<{WIKI_BASE_URL}/World_Bosses>)", reply)
        self.assertIn(f"[Bio-Android](<{WIKI_BASE_URL}/Bio-Android>)", reply)
        self.assertNotIn("dragonminez-faq", reply)

    def test_append_wiki_sources_skips_empty_reply_and_caps_links(self) -> None:
        from bulmaai.services.openai_client import (
            MAX_WIKI_SOURCE_LINKS,
            _append_wiki_sources,
        )

        self.assertEqual(
            _append_wiki_sources(
                "(no reply)",
                ["wiki--Raids.md"],
                base_url=WIKI_BASE_URL,
            ),
            "(no reply)",
        )

        filenames = [f"wiki--Page_{index}.md" for index in range(6)]
        reply = _append_wiki_sources(
            "answer",
            filenames,
            base_url=WIKI_BASE_URL,
        )
        self.assertEqual(reply.count("](<"), MAX_WIKI_SOURCE_LINKS)


if __name__ == "__main__":
    unittest.main()
