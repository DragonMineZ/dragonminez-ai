import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bulmaai.services.support_faq import (
    FAQCandidate,
    SupportFAQSource,
    normalize_faq_candidates,
    publish_faq_markdown_to_vector_store,
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
        return SimpleNamespace(output_text=json.dumps(self.payload))


class FakeFiles:
    def __init__(self) -> None:
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        file_arg = kwargs["file"]
        self.uploaded_name = getattr(file_arg, "name", "")
        self.uploaded_text = file_arg.read().decode("utf-8")
        return SimpleNamespace(id="file_faq")


class FakeVectorStoreFiles:
    def __init__(self) -> None:
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="vs_file_faq")


class FakeVectorStores:
    def __init__(self) -> None:
        self.files = FakeVectorStoreFiles()


class FakeOpenAIClient:
    def __init__(self, payload: dict | None = None) -> None:
        self.responses = FakeResponses(payload or {"candidates": []})
        self.files = FakeFiles()
        self.vector_stores = FakeVectorStores()


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

    async def test_suggest_faq_candidates_asks_openai_for_structured_candidates(self) -> None:
        source = SupportFAQSource(
            trace_id=7,
            created_at="2026-05-08T12:00:00Z",
            language="en",
            channel_id="456",
            question="How do I transform?",
            answer="Use the configured transform key.",
        )
        fake_client = FakeOpenAIClient(
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
            openai_client=fake_client,
            model="gpt-5.4-mini",
            max_candidates=3,
        )

        self.assertEqual(candidates[0].question, "How do I transform?")
        call = fake_client.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5.4-mini")
        self.assertEqual(call["metadata"]["workflow"], "support_faq_suggestion")
        self.assertEqual(call["store"], True)
        self.assertIn("FAQ candidates", call["instructions"])
        self.assertIn("How do I transform?", json.dumps(call["input"], ensure_ascii=False))

    async def test_publish_faq_markdown_uploads_file_to_vector_store(self) -> None:
        fake_client = FakeOpenAIClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            faq_path = Path(temp_dir) / "dragonminez-faq.md"
            faq_path.write_bytes(b"# FAQ\n\n## Q\nA\n")
            result = await publish_faq_markdown_to_vector_store(
                faq_path,
                vector_store_id="vs_faq",
                openai_client=fake_client,
            )

        self.assertEqual(result["file_id"], "file_faq")
        self.assertEqual(result["vector_store_file_id"], "vs_file_faq")
        self.assertEqual(fake_client.files.calls[0]["purpose"], "assistants")
        self.assertEqual(fake_client.files.uploaded_text, "# FAQ\n\n## Q\nA\n")
        self.assertEqual(
            fake_client.vector_stores.files.calls[0],
            {"vector_store_id": "vs_faq", "file_id": "file_faq"},
        )


if __name__ == "__main__":
    unittest.main()
