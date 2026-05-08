import os
import unittest
from pathlib import Path


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.config import get_editable_setting_names, load_settings


class FAQOpenAIKnowledgeTests(unittest.TestCase):
    def test_faq_review_cog_is_not_loaded_by_default(self) -> None:
        settings = load_settings(include_overrides=False)

        self.assertNotIn("bulmaai.cogs.faq_review", settings.initial_extensions)
        self.assertNotIn("faq_review_channel_id", get_editable_setting_names())

    def test_schema_does_not_create_database_faq_tables(self) -> None:
        schema = Path("scripts/schema.sql").read_text(encoding="utf-8")

        self.assertNotIn("CREATE TABLE IF NOT EXISTS faq_entries", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS faq_events", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS faq_review_candidates", schema)

    def test_knowledge_readme_documents_openai_faq_source(self) -> None:
        readme = Path("data/knowledge/README.md").read_text(encoding="utf-8").lower()

        self.assertIn("faq", readme)
        self.assertIn("openai vector store", readme)


if __name__ == "__main__":
    unittest.main()
