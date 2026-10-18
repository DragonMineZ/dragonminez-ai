import os
import unittest
from pathlib import Path


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.config import get_editable_setting_names, load_settings


class FAQKnowledgeTests(unittest.TestCase):
    def test_faq_review_cog_is_not_loaded_by_default(self) -> None:
        settings = load_settings(include_overrides=False)

        self.assertNotIn("bulmaai.cogs.faq_review", settings.initial_extensions)
        self.assertNotIn("faq_review_channel_id", get_editable_setting_names())

    def test_schema_does_not_create_database_faq_tables(self) -> None:
        schema = Path("scripts/schema.sql").read_text(encoding="utf-8")

        self.assertNotIn("CREATE TABLE IF NOT EXISTS faq_entries", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS faq_events", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS faq_review_candidates", schema)

    def test_knowledge_readme_documents_the_knowledge_store_and_faq(self) -> None:
        readme = Path("data/knowledge/README.md").read_text(encoding="utf-8").lower()

        self.assertIn("knowledge_docs", readme)
        self.assertIn("support-faq.md", readme)
        self.assertNotIn("openai", readme)

    def test_knowledge_sync_cog_is_loaded_and_the_wiki_action_is_gone(self) -> None:
        settings = load_settings(include_overrides=False)

        self.assertIn("bulmaai.cogs.knowledge_sync", settings.initial_extensions)
        self.assertFalse(Path(".github/workflows/wiki-sync.yml").exists())
        self.assertFalse(Path("scripts/sync_wiki_knowledge.py").exists())


if __name__ == "__main__":
    unittest.main()
