import os
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import support_info as cog_module
from bulmaai.cogs.ai_tickets import AITicketsCog
from bulmaai.services import support_info as db
from bulmaai.services import ticket_transcripts
from bulmaai.services.ticket_pages import StoredPage
from bulmaai.services.ticket_transcripts import SupportInfoSuggestion, TicketSummary, TranscriptLine
from bulmaai.ui import support_info_views as views
from bulmaai.ui.v2 import ALREADY_WORKING
from test_tickets import make_channel, make_cog as make_tickets_cog, make_ticket
from v2_helpers import buttons, sent_message, text

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
        knowledge = patch("bulmaai.cogs.ai_tickets.knowledge_file_id", AsyncMock(return_value=None))
        knowledge.start()
        self.addCleanup(knowledge.stop)
        self.support_cog = SimpleNamespace(suggest_from_ticket=AsyncMock())
        page = StoredPage(token="p" * 32, expires_at=None)
        cogs = {"SupportInfoCog": self.support_cog, "TicketsCog": SimpleNamespace(build_page=AsyncMock(return_value=page))}
        settings = types.SimpleNamespace(
            openai_ticket_summary_model="gpt-test",
            openai_ticket_vector_store_id=None,
            openai_support_vector_store_ids=(),
            ai_support_timeout_seconds=5,
            ai_ticket_close_delay_seconds=0,
            ai_ticket_escalation_role_ids=(),
            ai_ticket_category_id=77,
            ticket_transcript_public_url="https://tickets.example",
        )
        self.cog = AITicketsCog(
            types.SimpleNamespace(settings=settings, user=types.SimpleNamespace(id=999), get_cog=cogs.get)
        )
        self.suggestion = SupportInfoSuggestion("temporary", "Iris crash", "Update to 2.2.2.")

    async def close(self, lines, *, staff_solved: bool, suggestion="default") -> None:
        suggestion = self.suggestion if suggestion == "default" else suggestion
        summary = TicketSummary("Crash", "Crash.", "Updated.", True, (), False, suggestion)
        channel = SimpleNamespace(
            id=10, name="ticket-10", guild=SimpleNamespace(id=1), created_at=NOW, send=AsyncMock(), delete=AsyncMock()
        )
        self.cog._archived_channels.clear()
        with (
            patch.object(self.cog, "_collect_transcript", AsyncMock(return_value=(lines, 42))),
            patch("bulmaai.cogs.ai_tickets.summarize_ticket", AsyncMock(return_value=summary)),
            patch("bulmaai.cogs.ai_tickets.record_ticket_transcript", AsyncMock()),
            patch("bulmaai.cogs.ai_tickets.delete_image_analyses", AsyncMock()),
        ):
            await self.cog._close_ticket(
                channel,
                closed_by_id=5,
                requester_id=42,
                resolved=True,
                delete_channel=False,
                announce=False,
                staff_solved=staff_solved,
                ticket_id=42,
            )

    def lines(self, *kinds):
        return [TranscriptLine(NOW, kind, kind.title(), "text") for kind in kinds]

    async def test_staff_solved_close_suggests_even_without_staff_lines(self) -> None:
        await self.close(self.lines("requester", "assistant"), staff_solved=True)
        self.support_cog.suggest_from_ticket.assert_awaited_once_with(
            self.suggestion, ticket_id=42, page_link=f"https://tickets.example/t/{'p' * 32}"
        )

    async def test_no_suggestion_otherwise(self) -> None:
        await self.close(self.lines("requester", "staff"), staff_solved=False)
        await self.close(self.lines("requester", "staff"), staff_solved=True, suggestion=None)
        self.support_cog.suggest_from_ticket.assert_not_awaited()

    async def test_a_failing_suggestion_never_breaks_the_close(self) -> None:
        self.support_cog.suggest_from_ticket.side_effect = RuntimeError("db down")
        with patch("bulmaai.cogs.ai_tickets.record_ticket_transcript", AsyncMock()):
            await self.close(self.lines("staff"), staff_solved=True)
        self.assertIn(10, self.cog._archived_channels)

    async def test_tickets_archive_flags_only_the_solved_close(self) -> None:
        tickets = make_tickets_cog()
        ai_cog = SimpleNamespace(forget_archived=MagicMock(), _close_ticket=AsyncMock(return_value=True))
        tickets.bot.get_cog = lambda name: ai_cog
        await tickets._archive(make_channel(), make_ticket(), 5, True)
        self.assertTrue(ai_cog._close_ticket.await_args.kwargs["staff_solved"])
        self.assertEqual(ai_cog._close_ticket.await_args.kwargs["ticket_id"], 42)
        await tickets._archive(make_channel(), make_ticket(), 5, None)
        self.assertFalse(ai_cog._close_ticket.await_args.kwargs["staff_solved"])


class ReviewCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_content_and_buttons(self) -> None:
        view = views.review_card(entry(), page_link="https://tickets.example/t/abc", expiry_days=7)
        body = text(view)
        self.assertIn(views.REVIEW_HEAD, body)
        self.assertIn("**Kind** temporary", body)
        self.assertIn("**From ticket** #0042 · [transcript](https://tickets.example/t/abc)", body)
        self.assertIn("**Expires** 7 days after approval", body)
        self.assertIn("**2.2.1 crashes with Iris**", body)
        self.assertIn("Tell users to update to 2.2.2.", body)
        found = [(b.label, b.style, b.custom_id) for b in buttons(view)]
        self.assertEqual(
            found,
            [
                ("Approve", discord.ButtonStyle.success, "supinfo:approve:5"),
                ("Edit & approve", discord.ButtonStyle.secondary, "supinfo:edit:5"),
                ("Discard", discord.ButtonStyle.danger, "supinfo:discard:5"),
            ],
        )
        permanent = text(views.review_card(entry(kind="permanent", ticket_id=None)))
        self.assertNotIn("Expires", permanent)
        self.assertNotIn("From ticket", permanent)

    def test_custom_id_parsing(self) -> None:
        self.assertEqual(views.parse_review_custom_id("supinfo:approve:12"), ("approve", 12))
        for bad in ("supinfo:approve:x", "supinfo:nope:1", "other:approve:1", "supinfo", ""):
            self.assertIsNone(views.parse_review_custom_id(bad))

    async def test_handled_card_keeps_the_suggestion_and_drops_the_buttons(self) -> None:
        approved = views.review_handled_card(entry(status="active", updated_by=300))
        self.assertIn("2.2.1 crashes with Iris", text(approved))
        self.assertIn(entry().body, text(approved))
        self.assertIn("✅ Approved by <@300> · support info #5", text(approved))
        self.assertEqual(buttons(approved), [])
        self.assertIn("🗑️ Discarded by <@300>", text(views.review_handled_card(entry(status="discarded", updated_by=300))))


def make_cog(review_channel_id=None, mod_log=700):
    review_channel = MagicMock()
    review_channel.send = AsyncMock(return_value=SimpleNamespace(id=801))
    review_message = MagicMock()
    review_message.edit = AsyncMock()
    review_channel.get_partial_message = MagicMock(return_value=review_message)
    bot = MagicMock()
    bot.settings = SimpleNamespace(
        support_info_channel_id=555,
        support_info_review_channel_id=review_channel_id,
        moderation_log_channel_id=mod_log,
        discord_staff_role_ids=(STAFF_ROLE,),
        panel_guild_id=1,
        dev_guild_id=None,
    )
    bot.get_partial_messageable = MagicMock(return_value=review_channel)
    return cog_module.SupportInfoCog(bot), review_channel, review_message


def staff_member(*role_ids, guild_id=1):
    member = MagicMock(spec=discord.Member)
    member.id = 300
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.guild = SimpleNamespace(id=guild_id)
    return member


def click(user, custom_id: str, card=None):
    card = card or views.review_card(entry())
    interaction = MagicMock()
    interaction.type = discord.InteractionType.component
    interaction.data = {"custom_id": custom_id}
    interaction.user = user
    interaction.message = sent_message(card, message_id=801, channel_id=800)
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class SuggestFromTicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_stores_suggested_and_posts_the_card_without_pings(self) -> None:
        cog, channel, _ = make_cog(review_channel_id=None, mod_log=700)
        suggestion = SupportInfoSuggestion("temporary", "Iris crash", "Update.")
        with (
            patch.object(db, "add_info", AsyncMock(return_value=entry(review_channel_id=None, review_message_id=None))) as add,
            patch.object(db, "set_review_message", AsyncMock()) as set_review,
        ):
            await cog.suggest_from_ticket(suggestion, ticket_id=42, page_link=None)
        kwargs = add.await_args.kwargs
        self.assertEqual(
            (kwargs["status"], kwargs["source"], kwargs["ticket_id"], kwargs["created_by"], kwargs["kind"]),
            ("suggested", "ai", 42, None, "temporary"),
        )
        cog.bot.get_partial_messageable.assert_called_with(700)  # falls back to the mod log
        sent = channel.send.await_args.kwargs
        allowed = sent["allowed_mentions"]
        self.assertFalse(allowed.everyone or allowed.users or allowed.roles or allowed.replied_user)
        self.assertIn(views.REVIEW_HEAD, text(sent["view"]))
        set_review.assert_awaited_once_with(5, 700, 801)

    async def test_review_channel_setting_wins(self) -> None:
        cog, _, _ = make_cog(review_channel_id=900)
        with patch.object(db, "add_info", AsyncMock(return_value=entry())), patch.object(db, "set_review_message", AsyncMock()):
            await cog.suggest_from_ticket(SupportInfoSuggestion("permanent", "Q", "A"), ticket_id=None)
        cog.bot.get_partial_messageable.assert_called_with(900)


class FinishSuggestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_sets_the_default_expiry_and_collapses_the_card(self) -> None:
        cog, _, review_message = make_cog()
        approved = entry(status="active", updated_by=300)
        with patch.object(db, "set_status", AsyncMock(return_value=approved)) as set_status:
            result = await cog.finish_suggestion(5, status="active", by_id=300)
        self.assertIs(result, approved)
        kwargs = set_status.await_args.kwargs
        self.assertEqual(kwargs["only_from"], ("suggested",))
        self.assertAlmostEqual(
            (kwargs["default_expires_at"] - datetime.now(timezone.utc)).total_seconds(), 7 * 86400, delta=60
        )
        cog.bot.get_partial_messageable.assert_called_with(800)
        edit = review_message.edit.await_args.kwargs
        self.assertIn("✅ Approved by <@300> · support info #5", text(edit["view"]))
        self.assertEqual(buttons(edit["view"]), [])

    async def test_discard_has_no_expiry_and_lost_race_returns_none(self) -> None:
        cog, _, review_message = make_cog()
        with patch.object(db, "set_status", AsyncMock(return_value=None)) as set_status:
            self.assertIsNone(await cog.finish_suggestion(5, status="discarded", by_id=300))
        self.assertIsNone(set_status.await_args.kwargs["default_expires_at"])
        review_message.edit.assert_not_awaited()
        with self.assertRaises(ValueError):
            await cog.finish_suggestion(5, status="removed", by_id=300)

    async def test_card_edit_errors_are_ignored(self) -> None:
        cog, _, review_message = make_cog()
        review_message.edit.side_effect = discord.NotFound(MagicMock(status=404), "gone")
        with patch.object(db, "set_status", AsyncMock(return_value=entry(status="discarded", updated_by=300))):
            self.assertIsNotNone(await cog.finish_suggestion(5, status="discarded", by_id=300))

    async def test_set_status_sql_sets_expiry_only_for_temporary_without_one(self) -> None:
        fake = MagicMock()
        fake.fetchrow = AsyncMock(return_value=None)
        await db.set_status(5, "active", updated_by=1, default_expires_at=NOW, pool=fake)
        query, *args = fake.fetchrow.await_args.args
        self.assertIn("WHEN kind = 'temporary' AND expires_at IS NULL THEN $5", query)
        self.assertEqual(args[4], NOW)


class ReviewButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_staff_are_denied(self) -> None:
        cog, _, _ = make_cog()
        for user in (staff_member(), staff_member(STAFF_ROLE, guild_id=99)):
            interaction = click(user, "supinfo:approve:5")
            with patch.object(db, "set_status", AsyncMock()) as set_status:
                await cog.on_interaction(interaction)
            set_status.assert_not_awaited()
            interaction.response.send_message.assert_awaited_once_with(cog_module.STAFF_ONLY_MSG, ephemeral=True)

    async def test_other_custom_ids_are_ignored(self) -> None:
        cog, _, _ = make_cog()
        interaction = click(staff_member(STAFF_ROLE), "ticket_close")
        await cog.on_interaction(interaction)
        interaction.response.send_message.assert_not_awaited()
        interaction.response.edit_message.assert_not_awaited()

    async def test_approve_click_finishes_it(self) -> None:
        cog, _, review_message = make_cog()
        interaction = click(staff_member(STAFF_ROLE), "supinfo:approve:5")
        with patch.object(db, "set_status", AsyncMock(return_value=entry(status="active", updated_by=300))) as set_status:
            await cog.on_interaction(interaction)
        self.assertEqual(set_status.await_args.args[:2], (5, "active"))
        interaction.response.edit_message.assert_awaited_once()  # the busy state first
        self.assertIn("Approved by <@300>", text(review_message.edit.await_args.kwargs["view"]))
        interaction.followup.send.assert_not_awaited()

    async def test_second_click_gets_already_handled(self) -> None:
        cog, _, _ = make_cog()
        interaction = click(staff_member(STAFF_ROLE), "supinfo:discard:5")
        handled = entry(status="active", updated_by=301)
        with (
            patch.object(db, "set_status", AsyncMock(return_value=None)),
            patch.object(db, "get_info", AsyncMock(return_value=handled)),
        ):
            await cog.on_interaction(interaction)
        interaction.followup.send.assert_awaited_once_with(cog_module.ALREADY_HANDLED_MSG, ephemeral=True)
        # the card shows the winner's outcome, not the stale buttons
        self.assertIn("Approved by <@301>", text(interaction.edit_original_response.await_args.kwargs["view"]))

    async def test_double_click_while_working(self) -> None:
        cog, _, _ = make_cog()
        interaction = click(staff_member(STAFF_ROLE), "supinfo:approve:5")
        cog._working.add(801)
        await cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(ALREADY_WORKING, ephemeral=True)

    async def test_edit_opens_a_prefilled_modal_or_says_already_handled(self) -> None:
        cog, _, _ = make_cog()
        interaction = click(staff_member(STAFF_ROLE), "supinfo:edit:5")
        with patch.object(db, "get_info", AsyncMock(return_value=entry())):
            await cog.on_interaction(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual((modal.title_input.value, modal.body_input.value), (entry().title, entry().body))
        self.assertEqual([o.value for o in modal.kind_select.options if o.default], ["temporary"])

        interaction = click(staff_member(STAFF_ROLE), "supinfo:edit:5")
        with patch.object(db, "get_info", AsyncMock(return_value=entry(status="discarded"))):
            await cog.on_interaction(interaction)
        interaction.response.send_modal.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(cog_module.ALREADY_HANDLED_MSG, ephemeral=True)


class SuggestionModalTests(unittest.IsolatedAsyncioTestCase):
    def submit(self, modal, *, kind="permanent", days=""):
        modal.kind_select._selected_values = [kind]
        modal.days_input.value = days
        interaction = MagicMock()
        interaction.user = SimpleNamespace(id=300)
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_saves_then_approves(self) -> None:
        cog = SimpleNamespace(finish_suggestion=AsyncMock(return_value=entry(status="active")))
        modal = cog_module.SuggestionModal(cog, entry())
        interaction = self.submit(modal, kind="temporary", days="3")
        with patch.object(db, "update_info", AsyncMock(return_value=entry())) as update:
            await modal.callback(interaction)
        kwargs = update.await_args.kwargs
        self.assertEqual((kwargs["kind"], kwargs["only_from"]), ("temporary", ("suggested",)))
        self.assertAlmostEqual((kwargs["expires_at"] - datetime.now(timezone.utc)).total_seconds(), 3 * 86400, delta=60)
        cog.finish_suggestion.assert_awaited_once_with(5, status="active", by_id=300)
        interaction.followup.send.assert_awaited_once_with("Approved as support info #5.", ephemeral=True)

    async def test_bad_days_and_lost_race(self) -> None:
        cog = SimpleNamespace(finish_suggestion=AsyncMock())
        modal = cog_module.SuggestionModal(cog, entry())
        interaction = self.submit(modal, days="abc")
        with patch.object(db, "update_info", AsyncMock()) as update:
            await modal.callback(interaction)
        update.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(cog_module.DAYS_MSG, ephemeral=True)

        interaction = self.submit(modal)
        with patch.object(db, "update_info", AsyncMock(return_value=None)):
            await modal.callback(interaction)
        cog.finish_suggestion.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(cog_module.ALREADY_HANDLED_MSG, ephemeral=True)


if __name__ == "__main__":
    unittest.main()
