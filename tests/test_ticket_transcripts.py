import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.ticket_transcripts import (
    TicketSummary,
    TranscriptLine,
    anonymize_lines,
    backfill_ticket_knowledge,
    get_image_analyses,
    record_ticket_transcript,
    render_knowledge_markdown,
    render_summary_knowledge_markdown,
    render_transcript_text,
    save_ticket_knowledge,
    stored_knowledge_filename,
    summarize_ticket,
)


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.messages = SimpleNamespace(
            create=AsyncMock(
                return_value=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=json.dumps(payload))], stop_reason="end_turn"
                )
            )
        )


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
        fake_client = FakeClient(
            {
                "title": "  Transform key not binding  ",
                "problem": "Requester could not use the transform key.",
                "resolution": "Rebinding the key fixed it.",
                "resolved": True,
                "tags": ["Controls", "controls", "Keybinds"],
                "knowledge_worthy": True,
            }
        )

        summary = await summarize_ticket(_lines(), model="claude-haiku-5-5", client=fake_client)

        self.assertEqual(summary.title, "Transform key not binding")
        self.assertEqual(summary.tags, ("controls", "keybinds"))
        self.assertTrue(summary.resolved)
        self.assertTrue(summary.knowledge_worthy)

        call = fake_client.messages.create.call_args.kwargs
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(call["output_config"]["effort"], "low")
        self.assertIn("system", call)
        sent_transcript = call["messages"][0]["content"]
        self.assertNotIn("Goku", sent_transcript)
        self.assertIn("Requester", sent_transcript)

    async def test_summarize_ticket_returns_none_for_empty_lines_without_calling_client(self) -> None:
        fake_client = FakeClient({})

        summary = await summarize_ticket([], model="claude-haiku-5-5", client=fake_client)

        self.assertIsNone(summary)
        fake_client.messages.create.assert_not_called()

    async def test_save_ticket_knowledge_upserts_a_ticket_doc(self) -> None:
        upsert = AsyncMock(return_value=True)

        with patch("bulmaai.services.knowledge.upsert", upsert):
            filename = await save_ticket_knowledge(
                "# content", filename="ticket-20260926-42.md", title="Crash", pool="pool"
            )

        self.assertEqual(filename, "ticket-20260926-42.md")
        upsert.assert_awaited_once_with(
            "ticket-20260926-42.md", source="ticket", title="Crash", content="# content", pool="pool"
        )

    async def test_stored_knowledge_filename_reads_the_knowledge_filename_column(self) -> None:
        pool = SimpleNamespace(fetchval=AsyncMock(return_value="ticket-20260901-42.md"))

        self.assertEqual(await stored_knowledge_filename(42, pool=pool), "ticket-20260901-42.md")
        sql, channel_id = pool.fetchval.await_args.args
        self.assertIn("SELECT knowledge_filename", sql)
        self.assertEqual(channel_id, 42)

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


class RecordConnection:
    """Answers UPDATE with how many rows it hit; collects every statement."""

    def __init__(self, updated: int) -> None:
        self.updated = updated
        self.statements = []

    def transaction(self):
        return FakeAcquire(self)

    async def execute(self, sql, *args):
        self.statements.append((sql, args))
        return f"UPDATE {self.updated}" if sql.strip().startswith("UPDATE") else "INSERT 0 1"


class RecordTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def record(self, conn):
        await record_ticket_transcript(
            channel_id=900,
            guild_id=1,
            channel_name="closed-0042",
            requester_id=100,
            closed_by_id=200,
            resolved=False,
            ai_confidence=None,
            message_count=3,
            summary=None,
            transcript="text",
            knowledge_filename=None,
            html_token="k" * 32,
            html_expires_at=None,
            pool=FakePool(conn),
        )

    async def test_reclose_updates_the_channels_row_in_place(self) -> None:
        conn = RecordConnection(updated=1)
        await self.record(conn)
        self.assertEqual(len(conn.statements), 1)  # no DELETE, no INSERT: same id
        sql, args = conn.statements[0]
        self.assertIn("WHERE id = (SELECT max(id) FROM ticket_transcripts WHERE channel_id = $1)", sql)
        self.assertIn("COALESCE($15, knowledge_filename)", sql)  # an earlier close's knowledge doc is kept
        self.assertNotIn("openai_file_id", sql)
        self.assertEqual((args[0], args[15], args[16]), (900, "k" * 32, None))

    async def test_first_close_inserts(self) -> None:
        conn = RecordConnection(updated=0)
        await self.record(conn)
        self.assertEqual([sql.split()[0] for sql, _ in conn.statements], ["UPDATE", "INSERT"])
        self.assertFalse(any("DELETE" in sql for sql, _ in conn.statements))
        self.assertIn("knowledge_filename", conn.statements[1][0])
        self.assertNotIn("openai_file_id", conn.statements[1][0])


class BackfillPool:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.fetch_sql = ""
        self.executed = []

    async def fetch(self, sql, *args):
        self.fetch_sql = sql
        return self.rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class BackfillTicketKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def _row(self) -> dict:
        return {
            "id": 7,
            "channel_id": 42,
            "closed_at": datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            "title": "Crash on launch @everyone",
            "problem": "Game crashes, see https://evil.tld/x",
            "resolution": "Updated GeckoLib.",
            "tags": ["crash", "geckolib"],
            "resolved": True,
        }

    def test_summary_doc_has_the_header_but_no_conversation(self) -> None:
        markdown = render_summary_knowledge_markdown(self._row())

        self.assertTrue(markdown.startswith("# Support ticket: Crash on launch"))
        self.assertIn("closed 2026-09-01", markdown)
        self.assertIn("Outcome: Resolved", markdown)
        self.assertIn("Tags: crash, geckolib", markdown)
        self.assertIn("## Problem", markdown)
        self.assertIn("Updated GeckoLib.", markdown)
        self.assertNotIn("## Conversation", markdown)
        self.assertNotIn("evil.tld", markdown)
        self.assertNotIn("@everyone", markdown)

    async def test_backfill_upserts_each_old_row_and_sets_its_filename(self) -> None:
        pool = BackfillPool([self._row()])
        upsert = AsyncMock(return_value=True)

        with patch("bulmaai.services.knowledge.upsert", upsert):
            moved = await backfill_ticket_knowledge(pool=pool)

        self.assertEqual(moved, 1)
        self.assertIn("openai_file_id IS NOT NULL AND knowledge_filename IS NULL", pool.fetch_sql)
        filename = upsert.await_args.args[0]
        self.assertEqual(filename, "ticket-20260901-42.md")
        self.assertEqual(upsert.await_args.kwargs["source"], "ticket")
        self.assertTrue(upsert.await_args.kwargs["title"].startswith("Crash on launch"))
        self.assertNotIn("@everyone", upsert.await_args.kwargs["title"])
        self.assertNotIn("## Conversation", upsert.await_args.kwargs["content"])
        sql, args = pool.executed[0]
        self.assertIn("SET knowledge_filename = $2", sql)
        self.assertIn("knowledge_filename IS NULL", sql)
        self.assertEqual(args, (7, filename))

    async def test_backfill_with_nothing_left_does_nothing(self) -> None:
        pool = BackfillPool([])
        upsert = AsyncMock()

        with patch("bulmaai.services.knowledge.upsert", upsert):
            self.assertEqual(await backfill_ticket_knowledge(pool=pool), 0)
        upsert.assert_not_awaited()
        self.assertEqual(pool.executed, [])


if __name__ == "__main__":
    unittest.main()
