import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import knowledge


def _load_export_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "export_openai_knowledge.py"
    spec = importlib.util.spec_from_file_location("export_openai_knowledge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalDocTitleTests(unittest.TestCase):
    def test_title_is_the_first_heading_else_the_file_stem(self) -> None:
        self.assertEqual(knowledge.local_doc_title("a.md", "intro\n## Sub\n# Patreon roles \nbody"), "Patreon roles")
        self.assertEqual(knowledge.local_doc_title("patreon-discord-role-link.md", "no heading"), "patreon-discord-role-link")
        self.assertEqual(knowledge.local_doc_title("x.md", "#\n#NoSpace"), "x")


class SyncLocalDocsTests(unittest.IsolatedAsyncioTestCase):
    async def test_syncs_md_files_skips_readme_and_deletes_gone_ones(self) -> None:
        upsert = AsyncMock(side_effect=lambda filename, **kwargs: filename == "new.md")
        delete = AsyncMock(return_value=True)
        stored = {"same.md": "sha", "gone.md": "sha"}

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "README.md").write_text("# Readme", encoding="utf-8")
            (folder / "notes.txt").write_text("not markdown", encoding="utf-8")
            (folder / "new.md").write_text("# New doc\nbody", encoding="utf-8")
            (folder / "same.md").write_text("plain body", encoding="utf-8")
            with (
                patch.object(knowledge, "list_by_source", AsyncMock(return_value=stored)) as list_by_source,
                patch.object(knowledge, "upsert", upsert),
                patch.object(knowledge, "delete", delete),
            ):
                result = await knowledge.sync_local_docs(folder, pool="pool")

        list_by_source.assert_awaited_once_with("local", pool="pool")
        self.assertEqual(
            [(call.args[0], call.kwargs["title"], call.kwargs["source"]) for call in upsert.await_args_list],
            [("new.md", "New doc", "local"), ("same.md", "same", "local")],
        )
        self.assertEqual(upsert.await_args_list[0].kwargs["content"], "# New doc\nbody")
        delete.assert_awaited_once_with("gone.md", pool="pool")
        self.assertEqual(result, {"uploaded": 1, "deleted": 1, "unchanged": 1})

    def test_repo_knowledge_folder_is_where_the_cog_looks(self) -> None:
        from bulmaai.cogs.knowledge_sync import LOCAL_KNOWLEDGE_DIR

        self.assertTrue((LOCAL_KNOWLEDGE_DIR / "README.md").is_file())


class KnowledgeSyncCogTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self):
        from types import SimpleNamespace

        from bulmaai.cogs.knowledge_sync import KnowledgeSyncCog

        settings = SimpleNamespace(wiki_api_url="https://wiki.example/api.php", wiki_base_url="https://wiki.example/w")
        return KnowledgeSyncCog(SimpleNamespace(settings=settings, wait_until_ready=AsyncMock()))

    async def test_wiki_sync_logs_a_summary_and_never_raises(self) -> None:
        cog = self._cog()
        result = {"uploaded": 2, "deleted": 1, "unchanged": 30}
        with patch("bulmaai.cogs.knowledge_sync.sync_wiki_knowledge", AsyncMock(return_value=result)) as sync:
            with self.assertLogs("bulmaai.cogs.knowledge_sync", "INFO") as logs:
                await cog.wiki_sync.coro(cog)
        sync.assert_awaited_once_with("https://wiki.example/api.php", base_url="https://wiki.example/w")
        self.assertIn("uploaded 2, deleted 1, unchanged 30", logs.output[0])

        with patch("bulmaai.cogs.knowledge_sync.sync_wiki_knowledge", AsyncMock(side_effect=RuntimeError("down"))):
            with self.assertLogs("bulmaai.cogs.knowledge_sync", "ERROR"):
                await cog.wiki_sync.coro(cog)

    async def test_startup_syncs_local_docs_and_backfills_even_if_one_fails(self) -> None:
        cog = self._cog()
        with (
            patch.object(knowledge, "sync_local_docs", AsyncMock(side_effect=RuntimeError("db down"))) as local,
            patch("bulmaai.cogs.knowledge_sync.backfill_ticket_knowledge", AsyncMock(return_value=3)) as backfill,
            self.assertLogs("bulmaai.cogs.knowledge_sync", "INFO") as logs,
        ):
            await cog._before_wiki_sync()
        local.assert_awaited_once()
        backfill.assert_awaited_once()
        self.assertTrue(any("Backfilled 3" in line for line in logs.output))


class ExportSkipRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load_export_script()

    def test_wiki_and_ticket_files_are_skipped(self) -> None:
        skip = self.script.is_rebuilt_elsewhere
        self.assertTrue(skip("anything.md", {"source": "mediawiki"}))
        self.assertTrue(skip("anything.md", {"source": "github-wiki"}))
        self.assertTrue(skip("wiki--Raids.md", None))
        self.assertTrue(skip("ticket-20260901-42.md", {}))

    def test_manual_uploads_are_exported(self) -> None:
        skip = self.script.is_rebuilt_elsewhere
        self.assertFalse(skip("patreon-discord-role-link.md", None))
        self.assertFalse(skip("dragonminez-faq.md", {"source": "support-faq"}))
        self.assertFalse(skip("guide.pdf", "not-a-dict"))

    def test_export_filename_is_the_stem_as_markdown(self) -> None:
        self.assertEqual(self.script.export_filename("Server Guide.pdf", "file_1"), "Server Guide.md")
        self.assertEqual(self.script.export_filename("", "file_1"), "file_1.md")


if __name__ == "__main__":
    unittest.main()
