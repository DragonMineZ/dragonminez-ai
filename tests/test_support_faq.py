import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.support_faq import (
    FAQ_KNOWLEDGE_FILENAME,
    FAQCandidate,
    SupportFAQSource,
    normalize_faq_candidates,
    publish_faq_markdown,
    render_faq_markdown,
    suggest_faq_candidates,
    support_trace_to_faq_source,
    write_faq_markdown,
)


class FakeResponses:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))], stop_reason="end_turn"
        )


class FakeClient:
    def __init__(self, payload: dict | None = None) -> None:
        self.messages = FakeResponses(payload or {"candidates": []})


class SubscriptOnlyRow:
    def __init__(self, data: dict) -> None:
        self.data = data

    def __getitem__(self, key: str):
        return self.data[key]


class SupportFAQTests(unittest.IsolatedAsyncioTestCase):
    def test_support_trace_to_faq_source_keeps_only_support_context(self) -> None:
        row = {
            "id": 42,
            "created_at": "2026-05-08T12:00:00Z",
            "language": "en",
            "channel_id": 456,
            "user_id": 123,
            "reply_text": "Use the transform key after unlocking the form.",
            "input_json": [
                {"role": "developer", "content": "Conversation meta: channel_id=456"},
                {"role": "user", "content": "[requester Bruno id=123]\nHow do I transform?"},
                {"role": "assistant", "content": "Older assistant text"},
            ],
        }

        source = support_trace_to_faq_source(row)

        self.assertEqual(source.trace_id, 42)
        self.assertEqual(source.language, "en")
        self.assertEqual(source.question, "How do I transform?")
        self.assertEqual(source.answer, "Use the transform key after unlocking the form.")

    def test_support_trace_question_skips_knowledge_search_results(self) -> None:
        hit = {"type": "search_result", "source": "s", "title": "t", "content": [{"type": "text", "text": "wiki"}]}
        question = {"type": "text", "text": "[requester Bruno]\nHow do I transform?"}
        row = {"id": 1, "created_at": "x", "input_json": [{"role": "user", "content": [hit, question]}]}

        self.assertEqual(support_trace_to_faq_source(row).question, "How do I transform?")

    def test_support_trace_question_decodes_jsonb_text(self) -> None:
        row = {"id": 1, "created_at": "x", "input_json": json.dumps([{"role": "user", "content": "How do I transform?"}])}

        self.assertEqual(support_trace_to_faq_source(row).question, "How do I transform?")

    def test_support_trace_to_faq_source_accepts_subscript_only_rows(self) -> None:
        row = SubscriptOnlyRow(
            {
                "id": 42,
                "created_at": "2026-05-08T12:00:00Z",
                "language": "en",
                "channel_id": 456,
                "reply_text": "Use the transform key.",
                "input_json": [{"role": "user", "content": "How do I transform?"}],
            }
        )

        source = support_trace_to_faq_source(row)

        self.assertEqual(source.trace_id, 42)
        self.assertEqual(source.channel_id, "456")
        self.assertEqual(source.question, "How do I transform?")

    def test_normalize_faq_candidates_filters_invalid_and_deduplicates(self) -> None:
        payload = {
            "candidates": [
                {
                    "question": "How do I transform?",
                    "answer": "Use the configured transform key.",
                    "language": "en",
                    "tags": ["forms", "controls", "forms"],
                    "source_trace_ids": [7, "8", "bad"],
                    "confidence": 0.86,
                    "rationale": "Repeated support question.",
                },
                {
                    "question": "How do I transform?",
                    "answer": "Duplicate wording should be skipped.",
                    "confidence": 0.9,
                },
                {
                    "question": "What is your favorite color?",
                    "answer": "",
                    "confidence": 1,
                },
                {
                    "question": "Can I bypass staff?",
                    "answer": "No.",
                    "confidence": 0.2,
                },
            ]
        }

        candidates = normalize_faq_candidates(payload, min_confidence=0.6)

        self.assertEqual(
            candidates,
            [
                FAQCandidate(
                    question="How do I transform?",
                    answer="Use the configured transform key.",
                    language="en",
                    tags=("forms", "controls"),
                    source_trace_ids=(7, 8),
                    confidence=0.86,
                    rationale="Repeated support question.",
                )
            ],
        )

    def test_render_and_write_faq_markdown(self) -> None:
        candidate = FAQCandidate(
            question="How do I transform?",
            answer="Use the configured transform key.",
            language="en",
            tags=("forms", "controls"),
            source_trace_ids=(7, 8),
            confidence=0.86,
            rationale="Repeated support question.",
        )

        markdown = render_faq_markdown([candidate], title="DragonMineZ Generated FAQ")

        self.assertIn("# DragonMineZ Generated FAQ", markdown)
        self.assertIn("## How do I transform?", markdown)
        self.assertIn("Use the configured transform key.", markdown)
        self.assertIn("Language: en", markdown)
        self.assertIn("Tags: forms, controls", markdown)
        self.assertIn("Source traces: 7, 8", markdown)

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "generated" / "faq.md"
            write_faq_markdown([candidate], output)
            self.assertEqual(output.read_text(encoding="utf-8"), render_faq_markdown([candidate]))

    async def test_suggest_faq_candidates_asks_claude_for_structured_candidates(self) -> None:
        source = SupportFAQSource(
            trace_id=7,
            created_at="2026-05-08T12:00:00Z",
            language="en",
            channel_id="456",
            question="How do I transform?",
            answer="Use the configured transform key.",
        )
        fake_client = FakeClient(
            {
                "candidates": [
                    {
                        "question": "How do I transform?",
                        "answer": "Use the configured transform key.",
                        "language": "en",
                        "tags": ["forms"],
                        "source_trace_ids": [7],
                        "confidence": 0.9,
                    }
                ]
            }
        )

        candidates = await suggest_faq_candidates(
            [source],
            client=fake_client,
            model="claude-haiku-5-5",
            max_candidates=3,
        )

        self.assertEqual(candidates[0].question, "How do I transform?")
        call = fake_client.messages.calls[0]
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertIn("FAQ candidates", call["system"])
        self.assertEqual(call["messages"][0]["role"], "user")
        self.assertIn("How do I transform?", call["messages"][0]["content"])

    async def test_publish_faq_markdown_upserts_one_faq_doc(self) -> None:
        upsert = AsyncMock(return_value=True)
        markdown = "# DragonMineZ FAQ\n\n## Q\nA\n"

        with patch("bulmaai.services.knowledge.upsert", upsert):
            changed = await publish_faq_markdown(markdown, pool="pool")

        self.assertTrue(changed)
        upsert.assert_awaited_once_with(
            FAQ_KNOWLEDGE_FILENAME,
            source="faq",
            title="DragonMineZ FAQ",
            content=markdown,
            pool="pool",
        )
        self.assertEqual(FAQ_KNOWLEDGE_FILENAME, "support-faq.md")


if __name__ == "__main__":
    unittest.main()
