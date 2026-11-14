import os
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs import support_info as cog_module
from bulmaai.cogs.ai_tickets import AITicketsCog
from bulmaai.services import support_info as db
from bulmaai.services import ticket_transcripts
from bulmaai.services.ticket_pages import StoredPage
from bulmaai.services.ticket_transcripts import SupportInfoSuggestion, TicketSummary, TranscriptLine
from test_tickets import make_channel, make_cog as make_tickets_cog, make_ticket

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
STAFF_ROLE = 7


def entry(**overrides) -> db.SupportInfo:
    values = dict(
        id=5,
        kind="temporary",
        title="2.2.1 crashes with Iris",
        body="Tell users to update to 2.2.2.",
        status="suggested",
        source="ai",
        ticket_id=42,
        expires_at=None,
        created_by=None,
        updated_by=None,
        review_channel_id=800,
        review_message_id=801,
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(overrides)
    return db.SupportInfo(**values)


def summary_payload(support_info) -> dict:
    return {
        "title": "Crash",
        "problem": "Crash.",
        "resolution": "Updated.",
        "resolved": True,
        "tags": [],
        "knowledge_worthy": False,
        "support_info": support_info,
    }


class SummarySchemaTests(unittest.TestCase):
    def test_schema_is_strict_with_a_nullable_suggestion(self) -> None:
        schema = ticket_transcripts.TICKET_SUMMARY_SCHEMA
        self.assertEqual(sorted(schema["required"]), sorted(schema["properties"]))
        variants = schema["properties"]["support_info"]["anyOf"]
        self.assertIn({"type": "null"}, variants)
        obj = next(v for v in variants if v.get("type") == "object")
        self.assertFalse(obj["additionalProperties"])
        self.assertEqual(sorted(obj["required"]), ["body", "kind", "title"])
        self.assertEqual(obj["properties"]["kind"]["enum"], ["temporary", "permanent"])

    def test_parses_and_sanitizes_a_suggestion(self) -> None:
        summary = ticket_transcripts._normalize_ticket_summary(
            summary_payload(
                {
                    "kind": "temporary",
                    "title": "  Iris crash @everyone ",
                    "body": "Update to 2.2.2, see https://evil.tld/x",
                }
            )
        )
        suggestion = summary.support_info
        self.assertEqual(suggestion.kind, "temporary")
        self.assertEqual(suggestion.title, "Iris crash @​everyone")
        # Staff see the link on the review card before approving; the answer is often the link itself.
        self.assertIn("https://evil.tld/x", suggestion.body)

    def test_null_or_invalid_suggestion_is_dropped(self) -> None:
        for value in (
            None,
            "text",
            {"kind": "forever", "title": "t", "body": "b"},
            {"kind": "permanent", "title": "", "body": "b"},
            {"kind": "permanent", "title": "t", "body": "x" * (db.BODY_MAX + 1)},
        ):
            with self.subTest(value=value):
                self.assertIsNone(ticket_transcripts._normalize_ticket_summary(summary_payload(value)).support_info)
        self.assertIsNone(ticket_transcripts._normalize_ticket_summary({}).support_info)


class CloseSuggestionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        knowledge = patch("bulmaai.cogs.ai_tickets.stored_knowledge_filename", AsyncMock(return_value=None))
        knowledge.start()
        self.addCleanup(knowledge.stop)
        self.support_cog = SimpleNamespace(suggest_from_ticket=AsyncMock())
        page = StoredPage(token="p" * 32, expires_at=None)
        cogs = {"SupportInfoCog": self.support_cog, "TicketsCog": SimpleNamespace(build_page=AsyncMock(return_value=page))}
        settings = types.SimpleNamespace(
            ai_ticket_summary_model="gpt-test",
            ai_support_timeout_seconds=5,
            ai_ticket_escalation_role_ids=(),
            ai_ticket_category_id=77,
            ticket_transcript_public_url="https://tickets.example",
        )
        self.cog = AITicketsCog(
            types.SimpleNamespace(settings=settings, user=types.SimpleNamespace(id=999), get_cog=cogs.get)
        )
        self.suggestion = SupportInfoSuggestion("temporary", "Iris crash", "Update to 2.2.2.")

    async def close(self, lines, *, suggestion="default", recorded=True):
        """A close's archive step; returns the store_support_suggestion mock."""
        suggestion = self.suggestion if suggestion == "default" else suggestion
        summary = TicketSummary("Crash", "Crash.", "Updated.", True, (), False, suggestion)
        channel = SimpleNamespace(
            id=10, name="ticket-10", guild=SimpleNamespace(id=1), created_at=NOW, send=AsyncMock(), delete=AsyncMock()
        )
        self.cog._archived_channels.clear()
        record = AsyncMock() if recorded else AsyncMock(side_effect=RuntimeError("db down"))
        with (
            patch.object(self.cog, "_collect_transcript", AsyncMock(return_value=(lines, 42))),
            patch("bulmaai.cogs.ai_tickets.summarize_ticket", AsyncMock(return_value=summary)),
            patch("bulmaai.cogs.ai_tickets.record_ticket_transcript", record),
            patch("bulmaai.cogs.ai_tickets.delete_image_analyses", AsyncMock()),
            patch("bulmaai.cogs.ai_tickets.store_support_suggestion", AsyncMock()) as store,
        ):
            await self.cog._close_ticket(channel, closed_by_id=5, requester_id=42, resolved=True)
        return store

    def lines(self, *kinds):
        return [TranscriptLine(NOW, kind, kind.title(), "text") for kind in kinds]

    async def test_every_close_keeps_the_suggestion_for_a_staff_click_and_suggests_nothing_itself(self) -> None:
        store = await self.close(self.lines("requester", "assistant"))
        store.assert_awaited_once_with(10, self.suggestion)
        self.support_cog.suggest_from_ticket.assert_not_awaited()

    async def test_a_close_without_a_suggestion_clears_the_stored_one(self) -> None:
        store = await self.close(self.lines("requester", "staff"), suggestion=None)
        store.assert_awaited_once_with(10, None)

    async def test_nothing_is_stored_when_the_transcript_was_not_recorded(self) -> None:
        with self.assertLogs("bulmaai.cogs.ai_tickets", "ERROR"):
            store = await self.close(self.lines("staff"), recorded=False)
        store.assert_not_awaited()
        self.assertIn(10, self.cog._archived_channels)

    async def test_tickets_archive_passes_no_suggestion_flag(self) -> None:
        tickets = make_tickets_cog()
        ai_cog = SimpleNamespace(forget_archived=MagicMock(), _close_ticket=AsyncMock(return_value=True))
        tickets.bot.get_cog = lambda name: ai_cog
        for resolved in (True, None):
            await tickets._archive(make_channel(), make_ticket(), 5, resolved)
            self.assertEqual(
                ai_cog._close_ticket.await_args.kwargs, {"closed_by_id": 5, "requester_id": 100, "resolved": resolved}
            )


def make_cog():
    bot = MagicMock()
    bot.settings = SimpleNamespace(support_info_channel_id=555, discord_staff_role_ids=(STAFF_ROLE,), panel_guild_id=1)
    return cog_module.SupportInfoCog(bot)


class SuggestFromTicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_stores_suggested_and_posts_nothing(self) -> None:
        cog = make_cog()
        suggestion = SupportInfoSuggestion("temporary", "Iris crash", "Update.")
        with patch.object(db, "add_info", AsyncMock(return_value=entry())) as add:
            result = await cog.suggest_from_ticket(suggestion, ticket_id=42)
        self.assertEqual(result, entry())
        kwargs = add.await_args.kwargs
        self.assertEqual(
            (kwargs["status"], kwargs["source"], kwargs["ticket_id"], kwargs["created_by"], kwargs["kind"]),
            ("suggested", "ai", 42, None, "temporary"),
        )
        cog.bot.get_partial_messageable.assert_not_called()
        cog.bot.get_channel.assert_not_called()


class FinishSuggestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_sets_the_default_expiry(self) -> None:
        cog = make_cog()
        approved = entry(status="active", updated_by=300)
        with patch.object(db, "set_status", AsyncMock(return_value=approved)) as set_status:
            result = await cog.finish_suggestion(5, status="active", by_id=300)
        self.assertIs(result, approved)
        kwargs = set_status.await_args.kwargs
        self.assertEqual(kwargs["only_from"], ("suggested",))
        self.assertAlmostEqual(
            (kwargs["default_expires_at"] - datetime.now(timezone.utc)).total_seconds(), 7 * 86400, delta=60
        )
        cog.bot.get_partial_messageable.assert_not_called()

    async def test_discard_has_no_expiry_and_lost_race_returns_none(self) -> None:
        cog = make_cog()
        with patch.object(db, "set_status", AsyncMock(return_value=None)) as set_status:
            self.assertIsNone(await cog.finish_suggestion(5, status="discarded", by_id=300))
        self.assertIsNone(set_status.await_args.kwargs["default_expires_at"])
        with self.assertRaises(ValueError):
            await cog.finish_suggestion(5, status="removed", by_id=300)

    async def test_set_status_sql_sets_expiry_only_for_temporary_without_one(self) -> None:
        fake = MagicMock()
        fake.fetchrow = AsyncMock(return_value=None)
        await db.set_status(5, "active", updated_by=1, default_expires_at=NOW, pool=fake)
        query, *args = fake.fetchrow.await_args.args
        self.assertIn("WHEN kind = 'temporary' AND expires_at IS NULL THEN $5", query)
        self.assertEqual(args[4], NOW)


if __name__ == "__main__":
    unittest.main()
