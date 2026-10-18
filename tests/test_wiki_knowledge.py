import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.wiki_knowledge import (
    MEDIAWIKI_TITLES_PER_REQUEST,
    MEDIAWIKI_USER_AGENT,
    apply_wiki_sync,
    clean_wikitext,
    load_mediawiki_pages,
    plan_wiki_sync,
    render_wiki_knowledge_file,
    sync_wiki_knowledge,
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

    def test_subpage_filename_has_no_slash_and_roundtrips(self) -> None:
        filename = wiki_knowledge_filename("Forms/Super_Saiyan")
        self.assertEqual(filename, "wiki--Forms%2FSuper_Saiyan.md")
        self.assertEqual(wiki_slug_from_filename(filename), "Forms/Super_Saiyan")
        self.assertEqual(
            wiki_url_for_citation_filename(filename),
            ("Forms/Super Saiyan", f"{WIKI_BASE_URL}/Forms/Super_Saiyan"),
        )

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

    def test_strips_legacy_tvar_syntax(self) -> None:
        self.assertEqual(clean_wikitext("<translate>Press <tvar|key>G</> to charge.</translate>"), "Press G to charge.")

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
        stored = {
            "wiki--Raids.md": unchanged.content_sha256,
            "wiki--Sagas.md": "old-hash",
            "wiki--Deleted_Page.md": "whatever",
        }

        plan = plan_wiki_sync([unchanged, changed, new], stored)

        self.assertEqual([page.slug for page in plan.uploads], ["Sagas", "Dragons_and_Wishes"])
        self.assertEqual(plan.deletes, ("wiki--Deleted_Page.md",))
        self.assertEqual(plan.unchanged, 1)
        self.assertIn("wiki--Deleted_Page.md", stored)  # the caller's dict is left alone

    def test_plan_with_nothing_stored_uploads_everything(self) -> None:
        pages = [render_wiki_knowledge_file(slug="Races", body="hub")]

        plan = plan_wiki_sync(pages, {})

        self.assertEqual(len(plan.uploads), 1)
        self.assertEqual(plan.deletes, ())
        self.assertEqual(plan.unchanged, 0)


class ApplyWikiSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_upserts_pages_with_url_and_deletes_gone_ones(self) -> None:
        page = render_wiki_knowledge_file(slug="Forms/Super_Saiyan", body="Transform.")
        plan = plan_wiki_sync([page], {"wiki--Gone.md": "x"})
        upsert, delete = AsyncMock(return_value=True), AsyncMock(return_value=True)

        with patch("bulmaai.services.knowledge.upsert", upsert), patch("bulmaai.services.knowledge.delete", delete):
            result = await apply_wiki_sync(plan, pool="pool")

        self.assertEqual(result, {"uploaded": 1, "deleted": 1, "unchanged": 0})
        upsert.assert_awaited_once_with(
            "wiki--Forms%2FSuper_Saiyan.md",
            source="mediawiki",
            title="Forms/Super Saiyan",
            content=page.content,
            url=f"{WIKI_BASE_URL}/Forms/Super_Saiyan",
            pool="pool",
        )
        delete.assert_awaited_once_with("wiki--Gone.md", pool="pool")

    async def test_sync_compares_against_stored_mediawiki_rows(self) -> None:
        page = render_wiki_knowledge_file(slug="Raids", body="same")
        stored = {"wiki--Raids.md": page.content_sha256, "wiki--Old.md": "x"}
        list_by_source = AsyncMock(return_value=stored)
        upsert, delete = AsyncMock(), AsyncMock()

        with (
            patch("bulmaai.services.wiki_knowledge.load_mediawiki_pages", return_value=[page]) as load,
            patch("bulmaai.services.knowledge.list_by_source", list_by_source),
            patch("bulmaai.services.knowledge.upsert", upsert),
            patch("bulmaai.services.knowledge.delete", delete),
        ):
            result = await sync_wiki_knowledge(API_URL, base_url=WIKI_BASE_URL)

        load.assert_called_once_with(API_URL, base_url=WIKI_BASE_URL)
        list_by_source.assert_awaited_once_with("mediawiki", pool=None)
        upsert.assert_not_awaited()
        delete.assert_awaited_once_with("wiki--Old.md", pool=None)
        self.assertEqual(result, {"uploaded": 0, "deleted": 1, "unchanged": 1})

    async def test_sync_refuses_an_empty_wiki(self) -> None:
        delete = AsyncMock()
        with (
            patch("bulmaai.services.wiki_knowledge.load_mediawiki_pages", return_value=[]),
            patch("bulmaai.services.knowledge.delete", delete),
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing"):
                await sync_wiki_knowledge(API_URL)
        delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
