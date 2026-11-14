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
from aiohttp.test_utils import TestClient, TestServer

from bulmaai.cogs import support_report as cog_module
from bulmaai.services import ai_budget
from bulmaai.services import support_report as report
from bulmaai.ui.support_report_views import support_report_card
from bulmaai.ui.v2 import TEXT_LIMIT
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, RANDOM_ID, SECRET, make_bot
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


class StoredReportTests(unittest.IsolatedAsyncioTestCase):
    def test_numbers_are_what_the_card_shows_and_no_raw_text(self) -> None:
        rated = report.aggregate(
            tickets(),
            [trace(inp=1_000_000, out=200_000, cached=500_000)],
            [handoff_row("how do I fly?")],
            since=SINCE,
            until=MONDAY,
            feedback_rows=[dict(helpful=True, n=3), dict(helpful=False, n=1)],
        )
        numbers = report.numbers_json(rated)
        self.assertEqual(
            numbers,
            {
                "tickets_closed": 3,
                "tickets_resolved": 2,
                "tickets_unresolved": 1,
                "ai_replies": 1,
                "escalations": 0,
                "handoffs": 0,
                "log_diagnoses": 0,
                "spend": 0.205,
                "spend_by_model": [{"model": "claude-haiku-5-5", "cost": 0.205}],
                "helpful": 3,
                "not_helpful": 1,
                "helpful_percent": 75,
                "top_tags": [["crash", 3], ["java", 1], ["quests", 1]],
            },
        )
        dumped = json.dumps(numbers)
        for raw in ("Game crashes", "Update Java", "how do I fly?"):
            self.assertNotIn(raw, dumped)
        self.assertIsNone(report.numbers_json(sample_data())["helpful_percent"])

    def test_insights_json(self) -> None:
        self.assertIsNone(report.insights_json(None))
        insights = report.Insights(themes=["Crashes"], gaps=[("How to fly?", "Flight"), ("What is ki?", "")], notes="n")
        self.assertEqual(
            report.insights_json(insights),
            {
                "themes": ["Crashes"],
                "gaps": [{"question": "How to fly?", "page": "Flight"}, {"question": "What is ki?", "page": None}],
                "notes": "n",
            },
        )

    async def test_save_list_and_get(self) -> None:
        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=7)
        data = sample_data()
        self.assertEqual(await report.save_report(data, None, pool=pool), 7)
        sql, since, until, numbers, insights = pool.fetchval.await_args.args
        self.assertIn("INSERT INTO support_reports", sql)
        self.assertEqual((since, until, insights), (SINCE, MONDAY, None))
        self.assertEqual(json.loads(numbers), report.numbers_json(data))
        await report.save_report(data, report.Insights(["T"], [], ""), pool=pool)
        self.assertEqual(json.loads(pool.fetchval.await_args.args[4]), {"themes": ["T"], "gaps": [], "notes": ""})

        head = dict(id=7, since=SINCE, until=MONDAY, created_at=MONDAY)
        pool.fetch = AsyncMock(return_value=[head])
        self.assertEqual(await report.list_reports(pool=pool), [head])
        sql, limit = pool.fetch.await_args.args
        self.assertIn("ORDER BY created_at DESC", sql)
        self.assertEqual(limit, 26)

        pool.fetchrow = AsyncMock(return_value={**head, "data": json.dumps({"tickets_closed": 3}), "insights": None})
        found = await report.get_report(7, pool=pool)
        self.assertEqual((found["data"], found["insights"]), ({"tickets_closed": 3}, None))
        pool.fetchrow = AsyncMock(return_value=None)
        self.assertIsNone(await report.get_report(8, pool=pool))


def make_cog():
    bot = MagicMock()
    bot.settings = settings()
    return cog_module.SupportReportCog(bot)


def staff_member(*role_ids, guild_id=1):
    member = MagicMock(spec=discord.Member)
    member.id = 300
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.guild = SimpleNamespace(id=guild_id)
    return member


class CogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ai_budget.reset()

    async def tick(self, cog, *, last=None, claim=(True, None), ai_fails=False, now=MONDAY, save=None):
        insights = None if ai_fails else report.Insights(["Crashes"], [], "")
        save = save or AsyncMock(return_value=1)
        with (
            patch.object(report, "last_posted", AsyncMock(return_value=last)),
            patch.object(report, "claim_post", AsyncMock(return_value=claim)) as claim_mock,
            patch.object(report, "release_claim", AsyncMock()) as release_mock,
            patch.object(report, "collect", AsyncMock(return_value=sample_data())) as collect,
            patch.object(report, "generate_insights", AsyncMock(return_value=insights)),
            patch.object(report, "save_report", save),
        ):
            await cog._post_tick(now)
        self.collect = collect
        return claim_mock, release_mock, save

    async def test_saves_the_report_when_due(self) -> None:
        cog = make_cog()
        claim, release, save = await self.tick(cog)
        claim.assert_awaited_once()
        self.collect.assert_awaited_once_with(SINCE, MONDAY)
        data, insights = save.await_args.args
        self.assertEqual(data.tickets_closed, 3)
        self.assertEqual(insights.themes, ["Crashes"])
        release.assert_not_awaited()
        cog.bot.get_channel.assert_not_called()

    async def test_ai_failure_still_saves_the_numbers(self) -> None:
        cog = make_cog()
        _, _, save = await self.tick(cog, ai_fails=True)
        self.assertIsNone(save.await_args.args[1])

    async def test_not_due_or_lost_claim_saves_nothing(self) -> None:
        cog = make_cog()
        for kwargs in (dict(now=MONDAY.replace(hour=8)), dict(last=MONDAY - timedelta(hours=1)), dict(claim=(False, None))):
            _, _, save = await self.tick(cog, **kwargs)
            save.assert_not_awaited()

    async def test_a_failed_save_releases_the_claim(self) -> None:
        cog = make_cog()
        previous = MONDAY - timedelta(days=7)
        with (
            patch.object(report, "last_posted", AsyncMock(return_value=None)),
            patch.object(report, "claim_post", AsyncMock(return_value=(True, previous))),
            patch.object(report, "release_claim", AsyncMock()) as release,
            patch.object(report, "collect", AsyncMock(return_value=sample_data())),
            patch.object(report, "generate_insights", AsyncMock(return_value=None)),
            patch.object(report, "save_report", AsyncMock(side_effect=RuntimeError("db down"))),
            self.assertRaises(RuntimeError),
        ):
            await cog._post_tick(MONDAY)
        release.assert_awaited_once_with(previous)

    async def test_command_is_staff_only(self) -> None:
        cog = make_cog()
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
        cog = make_cog()
        ctx = MagicMock()
        ctx.author = staff_member(STAFF_ROLE)
        ctx.defer = AsyncMock()
        ctx.followup.send = AsyncMock()
        with (
            patch.object(report, "collect", AsyncMock(return_value=sample_data())),
            patch.object(report, "generate_insights", AsyncMock(return_value=None)),
            patch.object(report, "claim_post", AsyncMock()) as claim,
            patch.object(report, "save_report", AsyncMock()) as save,
        ):
            await cog_module.SupportReportCog.support_report.callback(cog, ctx)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(ctx.followup.send.call_args.kwargs["ephemeral"])
        ctx.followup.send.assert_awaited_once()
        claim.assert_not_called()
        save.assert_not_called()


# ---- panel API ---------------------------------------------------------------------------------------------------


class ReportsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(make_bot())))
        await self.client.start_server()
        patcher = patch("bulmaai.web.core.get_pool", AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id=HELPER_ID):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def test_list_newest_first(self):
        self.login()
        rows = [
            dict(id=2, since=SINCE, until=MONDAY, created_at=MONDAY),
            dict(id=1, since=SINCE - timedelta(days=7), until=SINCE, created_at=SINCE),
        ]
        with patch("bulmaai.web.routes_ai.report.list_reports", AsyncMock(return_value=rows)) as listed:
            response = await self.client.get("/api/ai/reports")
        self.assertEqual(response.status, 200, await response.text())
        listed.assert_awaited_once_with(26)
        self.assertEqual(
            (await response.json())["reports"],
            [
                {"id": 2, "since": SINCE.isoformat(), "until": MONDAY.isoformat(), "created_at": MONDAY.isoformat()},
                {
                    "id": 1,
                    "since": (SINCE - timedelta(days=7)).isoformat(),
                    "until": SINCE.isoformat(),
                    "created_at": SINCE.isoformat(),
                },
            ],
        )

    async def test_get_one(self):
        self.login()
        numbers = report.numbers_json(sample_data())
        insights = {"themes": ["Crashes"], "gaps": [{"question": "Fly?", "page": None}], "notes": ""}
        found = dict(id=2, since=SINCE, until=MONDAY, created_at=MONDAY, data=numbers, insights=insights)
        with patch("bulmaai.web.routes_ai.report.get_report", AsyncMock(return_value=found)) as get:
            response = await self.client.get("/api/ai/reports/2")
        self.assertEqual(response.status, 200, await response.text())
        get.assert_awaited_once_with(2)
        self.assertEqual(
            await response.json(),
            {
                "id": 2,
                "since": SINCE.isoformat(),
                "until": MONDAY.isoformat(),
                "created_at": MONDAY.isoformat(),
                "numbers": numbers,
                "insights": insights,
            },
        )

    async def test_unknown_or_bad_id_is_404(self):
        self.login()
        with patch("bulmaai.web.routes_ai.report.get_report", AsyncMock(return_value=None)) as get:
            self.assertEqual((await self.client.get("/api/ai/reports/9")).status, 404)
            response = await self.client.get("/api/ai/reports/abc")
        self.assertEqual(response.status, 404)
        self.assertEqual(await response.json(), {"error": "Unknown report."})
        get.assert_awaited_once_with(9)

    async def test_needs_a_staff_session(self):
        self.assertEqual((await self.client.get("/api/ai/reports")).status, 401)
        self.login(RANDOM_ID)
        self.assertEqual((await self.client.get("/api/ai/reports/1")).status, 401)


if __name__ == "__main__":
    unittest.main()
