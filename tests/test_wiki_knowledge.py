import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.wiki_knowledge import (
    RemoteWikiFile,
    load_wiki_pages,
    plan_wiki_sync,
    render_wiki_knowledge_file,
    wiki_knowledge_filename,
    wiki_page_title,
    wiki_page_url,
    wiki_slug_from_filename,
    wiki_url_for_citation_filename,
)


class WikiKnowledgeHelperTests(unittest.TestCase):
    def test_title_and_url_from_slug(self) -> None:
        self.assertEqual(wiki_page_title("Stats-and-Attributes"), "Stats and Attributes")
        self.assertEqual(
            wiki_page_url("Stats-and-Attributes"),
            "https://github.com/DragonMineZ/dragonminez/wiki/Stats-and-Attributes",
        )

    def test_filename_roundtrip(self) -> None:
        filename = wiki_knowledge_filename("Custom-Races")
        self.assertEqual(filename, "wiki--Custom-Races.md")
        self.assertEqual(wiki_slug_from_filename(filename), "Custom-Races")

    def test_slug_from_unmanaged_filename_is_none(self) -> None:
        self.assertIsNone(wiki_slug_from_filename("patreon-discord-role-link.md"))
        self.assertIsNone(wiki_slug_from_filename("Dragonmine_Z_Trello_ExportedScript.pdf"))
        self.assertIsNone(wiki_slug_from_filename(""))

    def test_citation_filename_maps_to_title_and_url(self) -> None:
        mapped = wiki_url_for_citation_filename("wiki--Dragons-and-Wishes.md")
        self.assertEqual(
            mapped,
            (
                "Dragons and Wishes",
                "https://github.com/DragonMineZ/dragonminez/wiki/Dragons-and-Wishes",
            ),
        )
        self.assertIsNone(wiki_url_for_citation_filename("dragonminez-faq.md"))


class WikiKnowledgeRenderTests(unittest.TestCase):
    def test_render_includes_title_source_and_body(self) -> None:
        page = render_wiki_knowledge_file(slug="Custom-Forms", body="Use JSON.\r\nReload after.")

        self.assertEqual(page.filename, "wiki--Custom-Forms.md")
        self.assertIn("# Custom Forms", page.content)
        self.assertIn(
            "Source: https://github.com/DragonMineZ/dragonminez/wiki/Custom-Forms",
            page.content,
        )
        self.assertIn("Use JSON.\nReload after.", page.content)
        self.assertEqual(len(page.content_sha256), 64)

    def test_render_hash_changes_with_body(self) -> None:
        first = render_wiki_knowledge_file(slug="Raids", body="v1")
        second = render_wiki_knowledge_file(slug="Raids", body="v2")

        self.assertNotEqual(first.content_sha256, second.content_sha256)


class LoadWikiPagesTests(unittest.TestCase):
    def test_loads_pages_and_skips_nav_and_empty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            (wiki_dir / "Home.md").write_text("Welcome", encoding="utf-8")
            (wiki_dir / "Custom-Races.md").write_text("Races doc", encoding="utf-8")
            (wiki_dir / "_Sidebar.md").write_text("nav", encoding="utf-8")
            (wiki_dir / "_Footer.md").write_text("nav", encoding="utf-8")
            (wiki_dir / "Empty-Page.md").write_text("   \n", encoding="utf-8")
            (wiki_dir / "image.png").write_bytes(b"\x89PNG")

            pages = load_wiki_pages(wiki_dir)

        self.assertEqual([page.slug for page in pages], ["Custom-Races", "Home"])


class PlanWikiSyncTests(unittest.TestCase):
    def test_plan_uploads_new_and_changed_and_deletes_stale(self) -> None:
        unchanged = render_wiki_knowledge_file(slug="Raids", body="same")
        changed = render_wiki_knowledge_file(slug="Sagas", body="new content")
        new = render_wiki_knowledge_file(slug="Dragons-and-Wishes", body="fresh")

        remote_unchanged = RemoteWikiFile(
            file_id="file-1", wiki_slug="Raids", content_sha256=unchanged.content_sha256
        )
        remote_changed = RemoteWikiFile(
            file_id="file-2", wiki_slug="Sagas", content_sha256="old-hash"
        )
        remote_stale = RemoteWikiFile(
            file_id="file-3", wiki_slug="Deleted-Page", content_sha256="whatever"
        )

        plan = plan_wiki_sync(
            [unchanged, changed, new],
            [remote_unchanged, remote_changed, remote_stale],
        )

        self.assertEqual([page.slug for page in plan.uploads], ["Sagas", "Dragons-and-Wishes"])
        self.assertEqual(
            sorted(remote.file_id for remote in plan.deletes),
            ["file-2", "file-3"],
        )
        self.assertEqual(plan.unchanged, 1)

    def test_plan_with_empty_remote_uploads_everything(self) -> None:
        pages = [render_wiki_knowledge_file(slug="Home", body="hub")]

        plan = plan_wiki_sync(pages, [])

        self.assertEqual(len(plan.uploads), 1)
        self.assertEqual(plan.deletes, ())
        self.assertEqual(plan.unchanged, 0)


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
            ["wiki--Raids.md", "dragonminez-faq.md"],
            base_url="https://github.com/DragonMineZ/dragonminez/wiki",
        )

        self.assertIn("Do the raid at night.", reply)
        self.assertIn(
            "[Raids](<https://github.com/DragonMineZ/dragonminez/wiki/Raids>)",
            reply,
        )
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
                base_url="https://github.com/DragonMineZ/dragonminez/wiki",
            ),
            "(no reply)",
        )

        filenames = [f"wiki--Page-{index}.md" for index in range(6)]
        reply = _append_wiki_sources(
            "answer",
            filenames,
            base_url="https://github.com/DragonMineZ/dragonminez/wiki",
        )
        self.assertEqual(reply.count("](<"), MAX_WIKI_SOURCE_LINKS)


if __name__ == "__main__":
    unittest.main()
