import os
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import knowledge


def pool(**returns) -> MagicMock:
    fake = MagicMock()
    fake.fetch = AsyncMock(return_value=returns.get("fetch", []))
    fake.execute = AsyncMock(return_value=returns.get("execute", "INSERT 0 1"))
    return fake


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_ors_the_words_and_maps_rows(self) -> None:
        fake = pool(
            fetch=[
                {"filename": "wiki-Ki.md", "title": "Ki", "url": "https://wiki.dragonminez.com/wiki/Ki", "text": "x" * 2000},
                {"filename": "ticket-1.md", "title": None, "url": None, "text": "Reinstall Forge."},
            ]
        )

        hits = await knowledge.search("  ¿cómo subo el ki?  ", 5, pool=fake)

        sql, *args = fake.fetch.await_args.args
        self.assertIn("websearch_to_tsquery('english', $1)", sql)
        self.assertIn("ts_rank_cd(tsv, q) DESC", sql)
        self.assertEqual(args, ["cómo or subo or el or ki", 5, knowledge.SEARCH_TEXT_CHARS])
        self.assertEqual(hits[0]["filename"], "wiki-Ki.md")
        self.assertEqual(hits[0]["url"], "https://wiki.dragonminez.com/wiki/Ki")
        self.assertEqual(len(hits[0]["text"]), 1500)
        self.assertEqual(hits[1], {"filename": "ticket-1.md", "title": "", "url": None, "text": "Reinstall Forge."})

    async def test_blank_query_skips_the_db(self) -> None:
        fake = pool()
        self.assertEqual(await knowledge.search("   ", 5, pool=fake), [])
        fake.fetch.assert_not_awaited()

    async def test_upsert_hashes_content_and_reports_changes(self) -> None:
        fake = pool(execute="INSERT 0 0")
        changed = await knowledge.upsert("faq.md", source="faq", title="FAQ", content="Q? A.", pool=fake)

        args = fake.execute.await_args.args
        self.assertFalse(changed)
        self.assertEqual(args[1:], ("faq.md", "faq", "FAQ", None, "Q? A.", knowledge.content_sha256("Q? A.")))

    async def test_list_by_source_maps_filename_to_sha(self) -> None:
        fake = pool(fetch=[{"filename": "a.md", "content_sha256": "abc"}])
        self.assertEqual(await knowledge.list_by_source("mediawiki", pool=fake), {"a.md": "abc"})
        self.assertEqual(fake.fetch.await_args.args[1], "mediawiki")


if __name__ == "__main__":
    unittest.main()
