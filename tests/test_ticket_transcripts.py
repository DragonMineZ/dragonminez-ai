import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.ticket_transcripts import (
    TicketSummary,
    TranscriptLine,
    anonymize_lines,
    get_image_analyses,
    render_knowledge_markdown,
    render_transcript_text,
    summarize_ticket,
    upload_ticket_knowledge,
)


class FakeFiles:
    def __init__(self) -> None:
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="file_ticket")


class FakeVectorStoreFiles:
    def __init__(self) -> None:
        self.calls = []

    async def create_and_poll(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="vs_file_ticket")


class FakeVectorStores:
    def __init__(self) -> None:
        self.files = FakeVectorStoreFiles()


class FakeOpenAIClient:
    def __init__(self, payload: dict) -> None:
        self.responses = SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(payload)))
        )
        self.files = FakeFiles()
        self.vector_stores = FakeVectorStores()


class FakeConnection:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.fetch_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self.rows


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


def _lines() -> list[TranscriptLine]:
    return [
        TranscriptLine(
            created_at=datetime(2026, 9, 26, 14, 3, tzinfo=timezone.utc),
            speaker_kind="requester",
            speaker_name="Goku",
            content="My transform key is not working",
        ),
        TranscriptLine(
            created_at=datetime(2026, 9, 26, 14, 5, tzinfo=timezone.utc),
            speaker_kind="staff",
            speaker_name="Vegeta",
            content="@Goku try rebinding the key in the config",
        ),
        TranscriptLine(
            created_at=datetime(2026, 9, 26, 14, 6, tzinfo=timezone.utc),
            speaker_kind="requester",
            speaker_name="Goku",
            content="Thanks Vegeta, that fixed it",
        ),
    ]


class TicketTranscriptTests(unittest.IsolatedAsyncioTestCase):
    def test_render_transcript_text_includes_names_kinds_and_timestamps(self) -> None:
        text = render_transcript_text(_lines(), channel_name="ticket-42")

        self.assertIn("Transcript: #ticket-42", text)
        self.assertIn("[2026-09-26 14:03 UTC] Goku (requester): My transform key is not working", text)
        self.assertIn("[2026-09-26 14:05 UTC] Vegeta (staff):", text)

    def test_anonymize_lines_replaces_requester_name_and_leaves_short_names(self) -> None:
        lines = list(_lines()) + [
            TranscriptLine(
                created_at=datetime(2026, 9, 26, 14, 7, tzinfo=timezone.utc),
                speaker_kind="requester",
                speaker_name="Al",
                content="Al here too",
            ),
        ]

        anonymized = anonymize_lines(lines)

        self.assertEqual(anonymized[0].speaker_name, "Requester")
        self.assertEqual(anonymized[1].speaker_name, "Staff")
        self.assertNotIn("Goku", anonymized[1].content)
        self.assertIn("Requester try rebinding", anonymized[1].content)
        self.assertIn("Thanks Vegeta, that fixed it", anonymized[2].content)
        # "Al" is shorter than 3 chars, so it is left untouched in content and label mapping.
        self.assertIn("Al here too", anonymized[3].content)

    def test_render_knowledge_markdown_has_summary_and_no_real_name(self) -> None:
        summary = TicketSummary(
            title="Transform key not binding",
            problem="Requester could not use the transform key.",
            resolution="Rebinding the key in the config fixed it.",
            resolved=True,
            tags=("controls", "keybinds"),
            knowledge_worthy=True,
        )

        markdown = render_knowledge_markdown(summary, _lines(), closed_at=datetime(2026, 9, 26, 15, 0))

        self.assertIn("# Support ticket: Transform key not binding", markdown)
        self.assertIn("Outcome: Resolved", markdown)
        self.assertIn("## Problem", markdown)
        self.assertIn("Requester could not use the transform key.", markdown)
        self.assertIn("## Resolution", markdown)
        self.assertIn("Rebinding the key in the config fixed it.", markdown)
        self.assertNotIn("Goku", markdown)

    async def test_summarize_ticket_returns_normalized_summary_with_anonymized_transcript(self) -> None:
        fake_client = FakeOpenAIClient(
            {
                "title": "  Transform key not binding  ",
                "problem": "Requester could not use the transform key.",
                "resolution": "Rebinding the key fixed it.",
                "resolved": True,
                "tags": ["Controls", "controls", "Keybinds"],
                "knowledge_worthy": True,
            }
        )

        summary = await summarize_ticket(_lines(), model="gpt-5-mini", openai_client=fake_client)

        self.assertEqual(summary.title, "Transform key not binding")
        self.assertEqual(summary.tags, ("controls", "keybinds"))
        self.assertTrue(summary.resolved)
        self.assertTrue(summary.knowledge_worthy)

        call = fake_client.responses.create.call_args.kwargs
        self.assertEqual(call["text"]["format"]["type"], "json_schema")
        self.assertEqual(call["text"]["format"]["name"], "ticket_summary")
        sent_transcript = call["input"][0]["content"]
        self.assertNotIn("Goku", sent_transcript)
        self.assertIn("Requester", sent_transcript)

    async def test_summarize_ticket_returns_none_for_empty_lines_without_calling_client(self) -> None:
        fake_client = FakeOpenAIClient({})

        summary = await summarize_ticket([], model="gpt-5-mini", openai_client=fake_client)

        self.assertIsNone(summary)
        fake_client.responses.create.assert_not_called()

    async def test_upload_ticket_knowledge_uploads_then_polls_vector_store(self) -> None:
        fake_client = FakeOpenAIClient({})

        file_id = await upload_ticket_knowledge(
            "# content",
            filename="ticket-20260926-42.md",
            vector_store_id="vs_tickets",
            attributes={"source": "ticket-transcript", "knowledge_worthy": True},
            openai_client=fake_client,
        )

        self.assertEqual(file_id, "file_ticket")
        self.assertEqual(fake_client.files.calls[0]["purpose"], "assistants")
        self.assertEqual(fake_client.files.calls[0]["file"][0], "ticket-20260926-42.md")
        poll_call = fake_client.vector_stores.files.calls[0]
        self.assertEqual(poll_call["vector_store_id"], "vs_tickets")
        self.assertEqual(poll_call["file_id"], "file_ticket")
        self.assertEqual(poll_call["attributes"]["knowledge_worthy"], True)

    async def test_get_image_analyses_empty_input_skips_pool(self) -> None:
        result = await get_image_analyses([])
        self.assertEqual(result, {})

    async def test_get_image_analyses_returns_mapping_from_pool(self) -> None:
        conn = FakeConnection(
            rows=[
                {"attachment_id": 111, "analysis": "A screenshot of the crash log."},
                {"attachment_id": 222, "analysis": "A screenshot of the inventory."},
            ]
        )

        result = await get_image_analyses([111, 222], pool=FakePool(conn))

        self.assertEqual(
            result,
            {111: "A screenshot of the crash log.", 222: "A screenshot of the inventory."},
        )
        sql, args = conn.fetch_calls[0]
        self.assertIn("ticket_image_analyses", sql)
        self.assertEqual(args[0], [111, 222])


if __name__ == "__main__":
    unittest.main()
