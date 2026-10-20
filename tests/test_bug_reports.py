import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from discord.components import _component_factory


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.config import load_settings
from bulmaai.cogs import bug_reports as bug_cog
from bulmaai.cogs.bug_reports import BugReportsCog
from bulmaai.services.bug_report_ai import (
    DuplicateAssessment,
    _coerce_triage,
    analyze_bug_report,
    assess_duplicate,
    log_digest,
)
from bulmaai.ui import bug_report_views as views
from bulmaai.utils.log_parser import LogReport


class BugReportConfigTests(unittest.TestCase):
    def test_bug_report_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "dummy-discord-token",
                "ANTHROPIC_API_KEY": "dummy-anthropic-key",
                "GH_APP_PRIVATE_KEY_PEM": "dummy-github-key",
            },
            clear=True,
        ):
            settings = load_settings(include_overrides=False)

        self.assertTrue(settings.bug_reports_enabled)
        self.assertEqual(settings.bug_report_forum_channel_id, 1526421736336265377)
        self.assertEqual(settings.bug_report_repo, "dragonminez")
        self.assertEqual(settings.bug_report_poll_minutes, 10)
        self.assertEqual(settings.ai_bugreport_model, "claude-haiku-5-5")

    def test_bug_report_settings_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BUG_REPORTS_ENABLED": "false",
                "BUG_REPORT_FORUM_CHANNEL_ID": "999",
                "BUG_REPORT_REPO": "dragonminez_ai",
                "BUG_REPORT_POLL_MINUTES": "3",
                "AI_BUGREPORT_MODEL": "claude-test",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertFalse(settings.bug_reports_enabled)
        self.assertEqual(settings.bug_report_forum_channel_id, 999)
        self.assertEqual(settings.bug_report_repo, "dragonminez_ai")
        self.assertEqual(settings.bug_report_poll_minutes, 3)
        self.assertEqual(settings.ai_bugreport_model, "claude-test")


class BugTriageCoercionTests(unittest.TestCase):
    def test_coerce_normalizes_invalid_severity_and_steps(self) -> None:
        triage = _coerce_triage(
            {
                "is_bug": True,
                "title": "Crash on transform",
                "summary": "Game crashes",
                "severity": "apocalyptic",
                "affected_area": "Transformations",
                "steps": ["Open menu", "  ", "Transform", 5],
            },
            fallback_title="fallback",
        )
        self.assertTrue(triage.is_bug)
        self.assertEqual(triage.severity, "medium")
        self.assertEqual(triage.steps, ["Open menu", "Transform", "5"])

    def test_coerce_strips_links_and_pings_the_bot_would_repost(self) -> None:
        triage = _coerce_triage(
            {
                "title": "Crash @everyone",
                "summary": "Staff-confirmed fix: [download hotfix](https://evil.tld/fix.jar)",
                "steps": ["Get it at https://evil.tld/x"],
            },
            fallback_title="fallback",
        )
        text = " ".join([triage.title, triage.summary, *triage.steps])
        self.assertNotIn("evil.tld", text)
        self.assertNotRegex(text, r"@(everyone|here)")
        self.assertIn("download hotfix", triage.summary)

    def test_coerce_uses_fallback_title_when_missing(self) -> None:
        triage = _coerce_triage({}, fallback_title="My Forum Post")
        self.assertFalse(triage.is_bug)
        self.assertEqual(triage.title, "My Forum Post")
        self.assertEqual(triage.severity, "medium")


class BugTriageCardTests(unittest.IsolatedAsyncioTestCase):
    def _sample_triage(self):
        return _coerce_triage(
            {
                "is_bug": True,
                "title": "Crash on transform",
                "summary": "Game crashes when transforming.",
                "severity": "high",
                "affected_area": "Transformations",
                "steps": ["Open the form menu", "Select Super Saiyan"],
            },
            fallback_title="fallback",
        )

    @staticmethod
    def _posted(view):
        """The card as Discord would hand it back on a message."""
        return SimpleNamespace(
            flags=SimpleNamespace(is_components_v2=True),
            components=[_component_factory(c) for c in view.to_components()],
        )

    def _card(self, **kwargs):
        return self._posted(views.triage_view(self._sample_triage(), thread_id=9, reporter_id=42, **kwargs))

    async def test_card_has_no_github_reference_and_names_the_reporter(self) -> None:
        text = views.message_text(self._card())
        self.assertNotIn("github", text.lower())
        self.assertIn("<@42>", text)

    async def test_card_text_round_trips_the_triage_details(self) -> None:
        text = views.message_text(self._card())
        self.assertIn("**Severity** High", text)
        self.assertIn("**Area** Transformations", text)
        self.assertIn("**Steps to reproduce**\n1. Open the form menu\n2. Select Super Saiyan", text)

    async def test_restatus_swaps_the_status_line_keeps_the_reporter_and_drops_buttons(self) -> None:
        for status, label in (("resolved", "Resolved"), ("duplicate", "duplicate"), ("fixed", "fixed")):
            view = views.restatus(self._card(), status)["view"]
            text = views.message_text(self._posted(view))
            self.assertIn(label, text.split("**Status**")[1])
            self.assertIn("<@42>", text)
            self.assertIsNone(view.get_item(views.ACTIONS_ID))

    async def test_card_renders_duplicate_and_fixed_suggestions(self) -> None:
        duplicate = DuplicateAssessment("duplicate", 123, "Crash when transforming", "high", "Same crash.")
        self.assertIn(f"**{views.DUPLICATE_TITLE}** #123", views.message_text(self._card(duplicate=duplicate)))
        fixed = DuplicateAssessment("already_fixed", 88, "Transform crash", "medium", "Fixed.")
        self.assertIn(f"**{views.FIXED_TITLE}** Closed issue #88", views.message_text(self._card(duplicate=fixed)))
        no_match = DuplicateAssessment("none", None, "", "low", "")
        text = views.message_text(self._card(duplicate=no_match))
        self.assertNotIn(views.DUPLICATE_TITLE, text)
        self.assertNotIn(views.FIXED_TITLE, text)

    async def test_game_breaking_report_suggests_a_ticket_with_a_button(self) -> None:
        triage = self._sample_triage()
        triage.game_breaking = True
        view = views.triage_view(triage, thread_id=9, reporter_id=42)
        self.assertIn("game-breaking", views.message_text(self._posted(view)))
        self.assertIn("bug_ticket:9", [b.custom_id for b in view.get_item(views.ACTIONS_ID).children])

        plain = views.triage_view(self._sample_triage(), thread_id=9, reporter_id=42)
        self.assertNotIn("game-breaking", views.message_text(self._posted(plain)))
        self.assertNotIn("bug_ticket:9", [b.custom_id for b in plain.get_item(views.ACTIONS_ID).children])


class LogDigestTests(unittest.TestCase):
    def test_digest_names_versions_and_the_error(self) -> None:
        report = LogReport(
            mc_version="1.20.1",
            dragonminez_version="2.1.3",
            errors=["[main/ERROR]: java.lang.NullPointerException at MovementSkillsHandler"],
            stacktrace="java.lang.NullPointerException\nat MovementSkillsHandler.tick",
        )
        digest = log_digest("latest.log", report)
        self.assertIn("Minecraft 1.20.1, DragonMineZ 2.1.3", digest)
        self.assertIn("MovementSkillsHandler", digest)


def _not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


class BugReportCogTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, tickets=None):
        cog = BugReportsCog.__new__(BugReportsCog)
        cog.bot = SimpleNamespace(get_cog=lambda name: tickets)
        cog.settings = SimpleNamespace()
        cog._opening_tickets = set()
        return cog

    async def test_starter_fetch_retries_until_the_post_exists(self) -> None:
        starter = object()
        thread = SimpleNamespace(id=9, starting_message=None, fetch_message=AsyncMock(side_effect=[_not_found(), _not_found(), starter]))
        with patch.object(bug_cog, "STARTER_FETCH_DELAY_SECONDS", 0):
            self.assertIs(await self._cog()._fetch_starter_message(thread), starter)
        self.assertEqual(thread.fetch_message.await_count, 3)

    def _interaction(self, user_id: int, thread):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            guild=SimpleNamespace(filesize_limit=10_000_000, get_member=lambda _id: None),
            channel=thread,
            message=None,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        )

    async def test_only_the_reporter_or_staff_can_open_the_ticket(self) -> None:
        tickets = SimpleNamespace(create_ticket=AsyncMock())
        report = SimpleNamespace(reporter_id=42, ai_title="Crash", ai_summary="")
        interaction = self._interaction(7, SimpleNamespace())
        with patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=report)), \
                patch.object(bug_cog, "is_staff", return_value=False):
            await self._cog(tickets)._handle_open_ticket(interaction, 9)
        tickets.create_ticket.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()

    async def test_reporter_gets_a_ticket_and_the_post_is_deleted(self) -> None:
        tickets = SimpleNamespace(create_ticket=AsyncMock(return_value=SimpleNamespace(name="bug-0007")))
        report = SimpleNamespace(reporter_id=42, ai_title="Crash on load", ai_summary="NPE")
        starter = SimpleNamespace(content="I attached my latest.log", attachments=[])
        thread = MagicMock(spec=discord.Thread)
        thread.name, thread.starting_message, thread.delete = "Crash on load", starter, AsyncMock()
        interaction = self._interaction(42, thread)
        with patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=report)), \
                patch.object(bug_cog, "set_status", AsyncMock()) as set_status:
            await self._cog(tickets)._handle_open_ticket(interaction, 9)
        args, kwargs = tickets.create_ticket.await_args
        self.assertIs(kwargs["owner"], interaction.user)
        self.assertIn(("What happened?", "I attached my latest.log"), args[2])
        set_status.assert_awaited_once_with(9, "ticket")
        thread.delete.assert_awaited_once()

    async def test_post_stays_when_the_ticket_could_not_be_opened(self) -> None:
        tickets = SimpleNamespace(create_ticket=AsyncMock(return_value=None))
        report = SimpleNamespace(reporter_id=42, ai_title="Crash", ai_summary="")
        thread = MagicMock(spec=discord.Thread)
        thread.name, thread.starting_message, thread.delete = "Crash", None, AsyncMock()
        thread.fetch_message = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no"))
        with patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=report)), \
                patch.object(bug_cog, "set_status", AsyncMock()) as set_status:
            await self._cog(tickets)._handle_open_ticket(self._interaction(42, thread), 9)
        set_status.assert_not_awaited()
        thread.delete.assert_not_awaited()


class BugReportIngestionTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self):
        cog = BugReportsCog.__new__(BugReportsCog)
        cog.bot = SimpleNamespace()
        cog.settings = SimpleNamespace(
            bug_reports_enabled=True, bug_report_forum_channel_id=500, ai_bugreport_model="claude-test", bug_report_repo=None
        )
        cog.client = object()
        return cog

    def _thread(self, starter):
        return SimpleNamespace(
            id=9, name="Crash on transform", parent_id=500, owner_id=42, starting_message=starter,
            guild=SimpleNamespace(id=1), send=AsyncMock(return_value=SimpleNamespace(id=1234)),
        )

    async def test_a_members_post_is_triaged_with_them_as_reporter(self) -> None:
        log_file = SimpleNamespace(filename="latest.log")
        starter = SimpleNamespace(content="It crashes when I transform", author=SimpleNamespace(id=42, bot=False), attachments=[log_file])
        thread = self._thread(starter)
        cog = self._cog()
        triage = _coerce_triage({"is_bug": True, "title": "Crash on transform"}, fallback_title="x")
        with patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=None)), \
                patch.object(bug_cog, "analyze_bug_report", AsyncMock(return_value=triage)) as analyze, \
                patch.object(bug_cog, "upsert_triage", AsyncMock()) as upsert, \
                patch.object(cog, "_log_digest", AsyncMock(return_value="digest")) as digest:
            await cog.on_thread_create(thread)
        digest.assert_awaited_once_with([log_file])
        kwargs = analyze.await_args.kwargs
        self.assertEqual(
            (kwargs["report_text"], kwargs["attachments"], kwargs["fallback_title"], kwargs["log_digest"]),
            ("Crash on transform\n\nIt crashes when I transform", ["latest.log"], "Crash on transform", "digest"),
        )
        self.assertEqual(thread.send.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        upsert.assert_awaited_once_with(
            thread_id=9, guild_id=1, reporter_id=42, triage_message_id=1234, ai_title="Crash on transform", ai_summary=""
        )

    async def test_posts_by_a_bot_or_already_triaged_are_left_alone(self) -> None:
        cog = self._cog()
        bot_starter = SimpleNamespace(content="Reported by <@42> with Bulma", author=SimpleNamespace(id=999, bot=True), attachments=[])
        with patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=None)), \
                patch.object(cog, "triage_thread", AsyncMock()) as triage:
            await cog.on_thread_create(self._thread(bot_starter))  # Bulma's report_bug post: AITicketsCog triages it
            triage.assert_not_called()
        with patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=SimpleNamespace())), \
                patch.object(cog, "triage_thread", AsyncMock()) as triage:
            await cog.on_thread_create(self._thread(SimpleNamespace(author=SimpleNamespace(id=42, bot=False))))
            triage.assert_not_called()

    async def test_triage_thread_takes_the_reporter_it_is_given(self) -> None:
        cog = self._cog()
        thread = self._thread(None)
        triage = _coerce_triage({"is_bug": False, "title": "Crash"}, fallback_title="x")
        with patch.object(bug_cog, "analyze_bug_report", AsyncMock(return_value=triage)) as analyze, \
                patch.object(bug_cog, "upsert_triage", AsyncMock()) as upsert:
            await cog.triage_thread(thread, text="Crash on transform\n\nReported by <@7> with Bulma", reporter_id=7, attachments=[])
        self.assertEqual((analyze.await_args.kwargs["attachments"], analyze.await_args.kwargs["log_digest"]), ([], ""))
        self.assertEqual(upsert.await_args.kwargs["reporter_id"], 7)
        self.assertEqual(upsert.await_args.kwargs["thread_id"], 9)


def _claude_json(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason)


class BugReportAICallTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_sends_claude_request_with_schema(self) -> None:
        create = AsyncMock(return_value=_claude_json('{"is_bug": true, "title": "Crash on load"}'))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))

        triage = await analyze_bug_report(client, model="claude-haiku-5-5", report_text="it crashes")

        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "it crashes"}])
        self.assertIn("system", kwargs)
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertGreaterEqual(kwargs["max_tokens"], 4000)
        self.assertEqual(triage.title, "Crash on load")

    async def test_refusal_falls_back_to_default_triage(self) -> None:
        client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=_claude_json("", "refusal"))))

        triage = await analyze_bug_report(client, model="claude-haiku-5-5", report_text="x", fallback_title="Fallback")

        self.assertEqual(triage.title, "Fallback")

    async def test_duplicate_assessment_reads_claude_json(self) -> None:
        payload = '{"match_type": "duplicate", "issue_number": 5, "confidence": "high", "reason": "same"}'
        create = AsyncMock(return_value=_claude_json(payload))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))

        result = await assess_duplicate(
            client, model="claude-haiku-5-5", report_text="boom", candidates=[{"number": 5, "title": "Boom"}]
        )

        self.assertEqual(result.issue_number, 5)
        self.assertEqual(create.await_args.kwargs["output_config"]["format"]["type"], "json_schema")

    async def test_duplicate_refusal_is_no_match(self) -> None:
        client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=_claude_json("", "refusal"))))

        result = await assess_duplicate(
            client, model="claude-haiku-5-5", report_text="boom", candidates=[{"number": 5, "title": "Boom"}]
        )

        self.assertEqual(result.match_type, "none")


if __name__ == "__main__":
    unittest.main()
