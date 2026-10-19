import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import support_report as cog_module
from bulmaai.services import ai_budget
from bulmaai.services import support_report as report
from bulmaai.ui.support_report_views import support_report_card
from bulmaai.ui.v2 import TEXT_LIMIT
from v2_helpers import text, texts

MONDAY = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
SINCE = MONDAY - timedelta(days=7)
STAFF_ROLE = 7


def trace(workflow="support_question", model="claude-haiku-5-5", inp=0, out=0, cached=0, kind="answer"):
    return dict(
        workflow=workflow, model=model, input_tokens=inp, output_tokens=out, cached_tokens=cached, reply_kind=kind
    )


def handoff_row(question, idx=1):
    return dict(
        id=idx,
        created_at=MONDAY,
        language="en",
        channel_id=5,
        reply_text="A staff member will help.",
        input_json=[
            {"role": "system", "content": "Context: x"},
            {"role": "user", "content": "[participant Bob · 1m ago]\nolder question"},
            {"role": "assistant", "content": "hm"},
            {"role": "user", "content": f"[participant Bob · 1m ago]\n{question}"},
        ],
    )


def tickets():
    return [
        dict(resolved=True, title="Crash on start", problem="Game crashes", resolution="Update Java", tags=["crash", "java"]),
        dict(resolved=True, title="Missing quest", problem="Quest gone", resolution="Relog", tags=["quests", "crash"]),
        dict(resolved=False, title="Server lag", problem="Lag", resolution="", tags=["crash"]),
    ]


def sample_data():
    traces = [
        trace(inp=1_000_000, out=200_000, cached=500_000),
        trace("support_escalation", "claude-sonnet-5-5", inp=100_000, out=10_000, kind="handoff"),
        trace("support_draft", inp=10, out=10),
        trace("log_diagnosis", inp=10, out=10),
    ]
    handoffs = [handoff_row("how do I fly?", 1), handoff_row("what is ki?", 2)]
    return report.aggregate(tickets(), traces, handoffs, since=SINCE, until=MONDAY)


class AggregateTests(unittest.TestCase):
    def test_ticket_counts_and_tags(self) -> None:
        data = sample_data()
        self.assertEqual((data.tickets_closed, data.tickets_resolved, data.tickets_unresolved), (3, 2, 1))
        self.assertEqual(data.top_tags[0], ("crash", 3))

    def test_reply_counts_by_workflow_and_handoffs(self) -> None:
        data = sample_data()
        self.assertEqual(data.ai_replies, 2)  # drafts and log diagnosis are not replies
        self.assertEqual(data.escalations, 1)
        self.assertEqual(data.handoffs, 1)
        self.assertEqual(data.replies_by_workflow["log_diagnosis"], 1)

    def test_spend_math_with_cached_tokens(self) -> None:
        # haiku: 1M*0.10 + 0.5M*0.10*0.1 + 0.2M*0.50 = 0.205
        self.assertAlmostEqual(report.estimate_cost("claude-haiku-5-5", 1_000_000, 200_000, 500_000), 0.205)
        # sonnet: 0.1M*2 + 0.01M*10 = 0.3
        self.assertAlmostEqual(report.estimate_cost("claude-sonnet-5-5", 100_000, 10_000, 0), 0.3)
        self.assertEqual(report.estimate_cost("some-other", 1, 1, 1), 0.0)
        data = sample_data()
        self.assertEqual(data.usage[0].model, "claude-sonnet-5-5")  # biggest spender first
        self.assertAlmostEqual(data.spend, 0.505012, places=5)

    def test_handoff_questions_are_the_last_user_message_truncated(self) -> None:
        data = sample_data()
        self.assertEqual(data.handoff_questions, ["how do I fly?", "what is ki?"])
        capped = report.aggregate([], [], [handoff_row("x" * 900)] * 20, since=SINCE, until=MONDAY)
        self.assertEqual(len(capped.handoff_questions), report.MAX_HANDOFFS)
        self.assertTrue(all(len(q) == report.QUESTION_MAX for q in capped.handoff_questions))

    def test_answer_feedback_counts_and_percent(self) -> None:
        rows = [dict(helpful=True, n=3), dict(helpful=False, n=1)]
        data = report.aggregate([], [], [], since=SINCE, until=MONDAY, feedback_rows=rows)
        self.assertEqual((data.helpful, data.not_helpful, data.ratings, data.helpful_percent), (3, 1, 4, 75))
        self.assertIsNone(sample_data().helpful_percent)

    def test_none_token_columns_count_as_zero(self) -> None:
        row = trace(inp=None, out=None, cached=None, kind=None)
        data = report.aggregate([], [row], [], since=SINCE, until=MONDAY)
        self.assertEqual(data.spend, 0.0)
        self.assertEqual(data.ai_replies, 1)


class CollectTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_counts_the_weeks_rated_answers(self) -> None:
        pool = MagicMock()

        async def fetch(sql, *args):
            if "FROM ai_feedback" in sql:
                self.assertEqual(args, (SINCE, MONDAY))
                self.assertIn("helpful IS NOT NULL", sql)
                return [dict(helpful=False, n=2), dict(helpful=True, n=6)]
            return []

        pool.fetch = fetch
        data = await report.collect(SINCE, MONDAY, pool=pool)
        self.assertEqual((data.helpful, data.not_helpful, data.helpful_percent), (6, 2, 75))


class ScheduleTests(unittest.TestCase):
    def test_monday_before_and_after_nine(self) -> None:
        self.assertFalse(report.report_due(MONDAY.replace(hour=8, minute=59), None))
        self.assertTrue(report.report_due(MONDAY.replace(hour=9), None))

    def test_other_weekdays_never(self) -> None:
        self.assertFalse(report.report_due(MONDAY + timedelta(days=1), None))
        self.assertFalse(report.report_due(MONDAY - timedelta(days=1), None))

    def test_recent_post_blocks_and_a_week_old_one_allows(self) -> None:
        self.assertFalse(report.report_due(MONDAY, MONDAY - timedelta(hours=1)))
        self.assertFalse(report.report_due(MONDAY, MONDAY - timedelta(days=5)))
        self.assertTrue(report.report_due(MONDAY, MONDAY - timedelta(days=7)))

    def test_restart_after_posting_does_not_repeat(self) -> None:
        # Posted at 09:05, bot restarts at 12:00 the same Monday: the DB stamp says no.
        self.assertFalse(report.report_due(MONDAY.replace(hour=12), MONDAY.replace(hour=9, minute=5)))


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_content_and_no_pings(self) -> None:
        insights = report.parse_insights(
            {
                "themes": ["Crashes on start @everyone", "Lag"],
                "gaps": [
                    {"question": "How do I fly <@123>? see https://evil.example/x", "suggested_wiki_page": "Flight"}
                ],
                "notes": "Spike on Friday.",
            }
        )
        out = text(support_report_card(sample_data(), insights))
        for needle in (
            "Weekly support report",
            "Tickets closed",
            "Support AI spend (est.)",
            "Recurring themes",
            "Questions the AI couldn't answer",
            "Flight",
            "**Notes**",
            "Nothing here goes live on its own",
        ):
            self.assertIn(needle, out)
        self.assertNotIn("<@123>", out)
        self.assertNotIn("evil.example", out)
        self.assertNotIn("@everyone", out)

    async def test_card_fits_the_limit_when_the_ai_is_wordy(self) -> None:
        insights = report.Insights(themes=["t" * 500] * 6, gaps=[("q" * 500, "p" * 200)] * 8, notes="n" * 2000)
        view = support_report_card(sample_data(), insights)
        out = text(view)
        self.assertLessEqual(sum(map(len, texts(view))), TEXT_LIMIT)
        self.assertIn("Nothing here goes live", out)

    async def test_card_without_insights_still_has_the_numbers(self) -> None:
        out = text(support_report_card(sample_data(), None))
        self.assertIn("Tickets closed", out)
        self.assertIn("No AI summary", out)

    async def test_answer_feedback_line_only_when_answers_were_rated(self) -> None:
        self.assertNotIn("Answer feedback", text(support_report_card(sample_data(), None)))
        rated = report.aggregate(
            tickets(), [], [], since=SINCE, until=MONDAY, feedback_rows=[dict(helpful=True, n=4), dict(helpful=False, n=1)]
        )
        self.assertIn("**Answer feedback** 👍 4 · 👎 1 (80% helpful)", text(support_report_card(rated, None)))


def settings(**overrides):
    values = dict(
        ai_support_escalation_model="claude-sonnet-5-5",
        ai_daily_small_token_limit=None,
        support_report_channel_id=900,
        discord_staff_role_ids=(STAFF_ROLE,),
        panel_guild_id=1,
        dev_guild_id=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def fake_response(payload):
    return SimpleNamespace(
        stop_reason="end_turn",
        model="claude-sonnet-5-5",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0
        ),
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


class InsightsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ai_budget.reset()

    async def test_success_sanitizes_and_records_usage(self) -> None:
        client = MagicMock()
        client.messages.create = AsyncMock(
            return_value=fake_response({"themes": ["Crashes"], "gaps": [], "notes": "see https://x.example/a"})
        )
        insights = await report.generate_insights(sample_data(), settings(), client=client)
        self.assertEqual(insights.themes, ["Crashes"])
        self.assertNotIn("x.example", insights.notes)
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-sonnet-5-5")
        self.assertEqual(kwargs["output_config"]["effort"], "medium")
        self.assertFalse(kwargs["output_config"]["format"]["schema"]["additionalProperties"])
        self.assertEqual(ai_budget.used("big"), 15)

    async def test_failure_returns_none(self) -> None:
        client = MagicMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
        self.assertIsNone(await report.generate_insights(sample_data(), settings(), client=client))

    async def test_skipped_when_paused_or_no_data(self) -> None:
        client = MagicMock()
        client.messages.create = AsyncMock()
        paused = settings(ai_daily_small_token_limit=0)
        self.assertIsNone(await report.generate_insights(sample_data(), paused, client=client))
        empty = report.aggregate([], [], [], since=SINCE, until=MONDAY)
        self.assertIsNone(await report.generate_insights(empty, settings(), client=client))
        client.messages.create.assert_not_called()


def make_cog(channel_found=True):
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.settings = settings()
    bot.get_channel = MagicMock(return_value=channel if channel_found else None)
    return cog_module.SupportReportCog(bot), channel


def staff_member(*role_ids, guild_id=1):
    member = MagicMock(spec=discord.Member)
    member.id = 300
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.guild = SimpleNamespace(id=guild_id)
    return member


class CogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ai_budget.reset()

    async def tick(self, cog, *, last=None, claim=(True, None), ai_fails=False, now=MONDAY):
        insights = None if ai_fails else report.Insights(["Crashes"], [], "")
        with (
            patch.object(report, "last_posted", AsyncMock(return_value=last)),
            patch.object(report, "claim_post", AsyncMock(return_value=claim)) as claim_mock,
            patch.object(report, "release_claim", AsyncMock()) as release_mock,
            patch.object(report, "collect", AsyncMock(return_value=sample_data())),
            patch.object(report, "generate_insights", AsyncMock(return_value=insights)),
        ):
            await cog._post_tick(now)
        return claim_mock, release_mock

    async def test_posts_when_due_without_pings(self) -> None:
        cog, channel = make_cog()
        await self.tick(cog)
        channel.send.assert_awaited_once()
        allowed = channel.send.call_args.kwargs["allowed_mentions"]
        self.assertFalse(allowed.everyone or allowed.users or allowed.roles)

    async def test_ai_failure_still_posts_the_numbers(self) -> None:
        cog, channel = make_cog()
        await self.tick(cog, ai_fails=True)
        out = text(channel.send.call_args.kwargs["view"])
        self.assertIn("Tickets closed", out)
        self.assertIn("No AI summary", out)

    async def test_not_due_or_disabled_or_lost_claim_posts_nothing(self) -> None:
        cog, channel = make_cog()
        await self.tick(cog, now=MONDAY.replace(hour=8))
        await self.tick(cog, last=MONDAY - timedelta(hours=1))
        await self.tick(cog, claim=(False, None))
        cog.bot.settings = settings(support_report_channel_id=None)
        await self.tick(cog)
        channel.send.assert_not_called()

    async def test_missing_channel_does_not_claim_and_logs_once(self) -> None:
        cog, _ = make_cog(channel_found=False)
        with self.assertLogs(cog_module.log, level="INFO") as logs:
            claim, _ = await self.tick(cog)
            await self.tick(cog)
        claim.assert_not_called()
        self.assertEqual(len(logs.records), 1)

    async def test_forbidden_skips_the_week_and_other_errors_release_the_claim(self) -> None:
        cog, channel = make_cog()
        channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "no")
        _, release = await self.tick(cog)
        release.assert_not_called()
        channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "oops")
        with self.assertRaises(discord.HTTPException):
            _, release = await self.tick(cog, claim=(True, MONDAY - timedelta(days=7)))

    async def test_command_is_staff_only(self) -> None:
        cog, _ = make_cog()
        command = cog_module.SupportReportCog.support_report.callback
        for member in (staff_member(99), staff_member(STAFF_ROLE, guild_id=2), SimpleNamespace(id=1)):
            ctx = MagicMock()
            ctx.author = member
            ctx.respond = AsyncMock()
            ctx.defer = AsyncMock()
            await command(cog, ctx)
            ctx.respond.assert_awaited_once()
            self.assertTrue(ctx.respond.call_args.kwargs["ephemeral"])
            ctx.defer.assert_not_called()

    async def test_command_sends_ephemeral_report_without_recording_a_post(self) -> None:
        cog, _ = make_cog()
        ctx = MagicMock()
        ctx.author = staff_member(STAFF_ROLE)
        ctx.defer = AsyncMock()
        ctx.followup.send = AsyncMock()
        with (
            patch.object(report, "collect", AsyncMock(return_value=sample_data())),
            patch.object(report, "generate_insights", AsyncMock(return_value=None)),
            patch.object(report, "claim_post", AsyncMock()) as claim,
        ):
            await cog_module.SupportReportCog.support_report.callback(cog, ctx)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(ctx.followup.send.call_args.kwargs["ephemeral"])
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
