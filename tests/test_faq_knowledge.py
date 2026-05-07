import os
import unittest


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.faq_knowledge import (
    FAQEntryInput,
    content_hash,
    render_faq_text,
    upsert_approved_faq,
    validate_lang,
    validate_status,
)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self) -> None:
        self.fetchval_calls = []
        self.execute_calls = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        return 42

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"


class FakeAcquire:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.conn)


class FAQKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def test_render_faq_text_includes_question_answer_and_tags(self) -> None:
        rendered = render_faq_text(
            canonical_question="How do I transform?",
            answer="Use the configured form key.",
            tags=["forms", "controls"],
        )

        self.assertEqual(
            rendered,
            "Question: How do I transform?\n\n"
            "Answer: Use the configured form key.\n\n"
            "Tags: forms, controls",
        )

    def test_content_hash_is_stable_after_whitespace_normalization(self) -> None:
        first = content_hash(
            lang="EN",
            canonical_question=" How   do I transform? ",
            answer="Use the configured\nform key.",
            tags=["controls", "forms"],
        )
        second = content_hash(
            lang="en",
            canonical_question="How do I transform?",
            answer="Use the configured form key.",
            tags=["forms", "controls"],
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_validate_status_and_lang_reject_invalid_values(self) -> None:
        self.assertEqual(validate_status(" APPROVED "), "approved")
        self.assertEqual(validate_lang("PT-BR"), "pt-br")

        with self.assertRaises(ValueError):
            validate_status("candidate")
        with self.assertRaises(ValueError):
            validate_lang("english")

    async def test_upsert_approved_faq_writes_entry_embedding_version_and_event(self) -> None:
        conn = FakeConnection()
        pool = FakePool(conn)
        embedded_texts = []

        async def embedding_provider(texts, model):
            embedded_texts.extend(texts)
            return [[0.1, 0.2, 0.3]]

        entry = FAQEntryInput(
            lang="en",
            canonical_question="How do I transform?",
            answer="Use the configured form key.",
            tags=["forms"],
            source_ticket_channel_id=123,
            source_question_message_ids=[456],
            source_answer_message_ids=[789],
            approved_by=99,
        )

        result = await upsert_approved_faq(
            entry,
            embedding_provider=embedding_provider,
            embedding_model="test-embedding",
            pool=pool,
        )

        self.assertEqual(result.faq_id, 42)
        self.assertEqual(result.dimensions, 3)
        self.assertEqual(embedded_texts, [render_faq_text(**entry.render_kwargs())])
        self.assertEqual(len(conn.fetchval_calls), 1)
        self.assertEqual(len(conn.execute_calls), 3)
        self.assertIn("INSERT INTO faq_entries", conn.fetchval_calls[0][0])
        self.assertIn("ON CONFLICT (lang, content_hash)", conn.fetchval_calls[0][0])
        self.assertIn("INSERT INTO faq_embeddings", conn.execute_calls[0][0])
        self.assertIn("INSERT INTO knowledge_base_version", conn.execute_calls[1][0])
        self.assertIn("INSERT INTO faq_events", conn.execute_calls[2][0])

    def test_schema_defines_separate_faq_tables(self) -> None:
        with open("scripts/schema.sql", encoding="utf-8") as handle:
            schema = handle.read()

        self.assertIn("CREATE TABLE IF NOT EXISTS faq_entries", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS faq_embeddings", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS faq_events", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS knowledge_base_version", schema)
        self.assertIn("REFERENCES faq_entries(id) ON DELETE CASCADE", schema)


if __name__ == "__main__":
    unittest.main()
