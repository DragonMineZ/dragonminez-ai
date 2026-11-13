import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import ai_daily

DAY = date(2026, 11, 12)
SETTINGS = SimpleNamespace(
    ai_budget_cycle_day=1,
    ai_monthly_budget_usd=100.0,
    ai_daily_small_token_limit=5_000_000,
    ai_daily_big_token_limit=None,
)


def ago(days: int) -> date:
    return DAY - timedelta(days=days)


def answers(day: date, n: int, kind="answer", workflow="support_question") -> dict:
    return dict(day=day, workflow=workflow, reply_kind=kind, n=n)


class CostGroupTests(unittest.TestCase):
    def test_one_feature_of_each_group(self) -> None:
        expected = {
            "support_question": "support",
            "support_draft": "support",
            "log_diagnosis_escalation": "support",
            "image_reading": "support",
            "ticket_summary": "tickets",
            "ticket_owner_translation": "tickets",
            "staff_assistant": "staff",
            "staff_distill": "staff",
            "weekly_report": "other",
            "code_notes": "other",
            "eval_judge": "other",
            "": "other",
        }
        for feature, group in expected.items():
            self.assertEqual(ai_daily.cost_group(feature), group, feature)
        self.assertEqual(ai_daily.cost_group(None), "other")


class AggregateTests(unittest.TestCase):
    def test_empty_input_zero_fills_the_window(self) -> None:
        data = ai_daily.aggregate(DAY, SETTINGS)

        self.assertEqual(len(data.points), ai_daily.WINDOW_DAYS)
        self.assertEqual([point.day for point in data.points], [ago(13 - index) for index in range(14)])
        self.assertIs(data.today, data.points[-1])
        self.assertEqual(data.today.day, DAY)
        for point in data.points:
            self.assertEqual(point.answers, dict.fromkeys(ai_daily.ANSWER_KINDS, 0))
            self.assertEqual(point.cost, dict.fromkeys(ai_daily.COST_GROUPS, 0.0))
            self.assertEqual((point.answers_total, point.cost_total, point.escalated), (0, 0, 0))
        self.assertFalse(data.has_activity)
        self.assertFalse(data.partial)
        self.assertIsNone(data.answers_avg7)
        self.assertIsNone(data.answers_delta)
        self.assertEqual(data.pool_used, {"small": 0, "big": 0})
        self.assertEqual(data.pool_limit, {"small": 5_000_000, "big": None})
        self.assertEqual((data.cycle_start, data.cycle_end), (date(2026, 11, 1), date(2026, 12, 1)))
        self.assertEqual(data.monthly_budget, 100.0)

    def test_days_sets_the_window(self) -> None:
        data = ai_daily.aggregate(DAY, SETTINGS, answer_rows=[answers(ago(29), 4), answers(ago(30), 99)], days=30)

        self.assertEqual(len(data.points), 30)
        self.assertEqual([point.day for point in data.points], [ago(29 - index) for index in range(30)])
        self.assertEqual(data.points[0].answers_total, 4)
        self.assertEqual(sum(point.answers_total for point in data.points), 4)
        for point in data.points[1:]:
            self.assertEqual(point.answers, dict.fromkeys(ai_daily.ANSWER_KINDS, 0))
            self.assertEqual((point.cost_total, point.tickets_opened, point.helpful), (0, 0, 0))
        # The average still looks at the 7 days before `day` only.
        self.assertIsNone(data.answers_avg7)

        short = ai_daily.aggregate(DAY, SETTINGS, answer_rows=[answers(ago(1), 6), answers(ago(2), 2)], days=3)
        self.assertEqual(len(short.points), 3)
        self.assertEqual(short.answers_avg7, 4.0)

    def test_rows_land_on_their_day(self) -> None:
        data = ai_daily.aggregate(
            DAY,
            SETTINGS,
            answer_rows=[
                answers(DAY, 5),
                answers(DAY, 2, "clarify"),
                answers(DAY, 1, "handoff", "support_escalation"),
                answers(DAY, 3, "answer", "support_escalation"),
                answers(DAY, 4, "offtopic"),
                answers(DAY, 1, None),
                answers(DAY, 1, ""),
                answers(ago(1), 7),
                answers(ago(14), 99),  # outside the window
            ],
            cost_rows=[
                dict(day=DAY, feature="support_question", cost=Decimal("1.25")),
                dict(day=DAY, feature="log_diagnosis", cost=Decimal("0.25")),
                dict(day=DAY, feature="ticket_summary", cost=Decimal("0.5")),
                dict(day=DAY, feature="staff_assistant", cost=Decimal("0.125")),
                dict(day=DAY, feature="weekly_report", cost=Decimal("2")),
                dict(day=ago(13), feature="image_reading", cost=Decimal("0.75")),
            ],
            opened_rows=[dict(day=DAY, n=6), dict(day=ago(2), n=1)],
            closed_rows=[dict(day=DAY, closed=4, resolved=3)],
            feedback_rows=[dict(day=DAY, helpful=True, n=8), dict(day=DAY, helpful=False, n=2)],
            pool_rows=[
                dict(model="claude-haiku-5-5", tokens=1000),
                dict(model="claude-sonnet-5-5", tokens=200),
                dict(model="mystery", tokens=5),
            ],
            cycle_spend=Decimal("12.5"),
            suggestions_waiting=3,
            answers_unreviewed=4,
            partial=True,
        )
        today = data.today

        self.assertEqual(today.answers, {"answer": 8, "clarify": 2, "handoff": 1, "other": 6})
        self.assertEqual(today.answers_total, 17)
        self.assertEqual(today.escalated, 4)
        self.assertEqual(today.cost, {"support": 1.5, "tickets": 0.5, "staff": 0.125, "other": 2.0})
        self.assertIsInstance(today.cost["support"], float)
        self.assertEqual(today.cost_total, 4.125)
        self.assertEqual((today.tickets_opened, today.tickets_closed, today.tickets_resolved), (6, 4, 3))
        self.assertEqual((today.helpful, today.not_helpful), (8, 2))
        self.assertEqual(data.points[-2].answers["answer"], 7)
        self.assertEqual(data.points[-3].tickets_opened, 1)
        self.assertEqual(data.points[0].cost["support"], 0.75)
        self.assertEqual(sum(point.answers_total for point in data.points), 24)
        self.assertEqual(data.pool_used, {"small": 1000, "big": 205})
        self.assertEqual(data.cycle_spend, 12.5)
        self.assertIsInstance(data.cycle_spend, float)
        self.assertEqual((data.suggestions_waiting, data.answers_unreviewed), (3, 4))
        self.assertTrue(data.partial)
        self.assertTrue(data.has_activity)

    def test_average_and_delta_use_the_seven_days_before(self) -> None:
        rows = [answers(ago(offset), 10) for offset in range(1, 8)]
        rows += [answers(DAY, 15), answers(ago(8), 1000)]  # day 8 back is not in the average
        data = ai_daily.aggregate(DAY, SETTINGS, answer_rows=rows)

        self.assertEqual(data.answers_avg7, 10.0)
        self.assertAlmostEqual(data.answers_delta, 0.5)

        only_today = ai_daily.aggregate(DAY, SETTINGS, answer_rows=[answers(DAY, 3)])
        self.assertIsNone(only_today.answers_avg7)
        self.assertIsNone(only_today.answers_delta)

        one_day = ai_daily.aggregate(DAY, SETTINGS, answer_rows=[answers(ago(3), 7)])
        self.assertEqual(one_day.answers_avg7, 1.0)
        self.assertEqual(one_day.answers_delta, -1.0)

    def test_projected_spend(self) -> None:
        # 12 of November's 30 days elapsed.
        data = ai_daily.aggregate(DAY, SETTINGS, cycle_spend=Decimal("24"))
        self.assertAlmostEqual(data.projected_spend, 60.0)

        mid = SimpleNamespace(ai_budget_cycle_day=12, ai_monthly_budget_usd=50)
        first_day = ai_daily.aggregate(DAY, mid, cycle_spend=2)
        self.assertEqual((first_day.cycle_start, first_day.cycle_end), (date(2026, 11, 12), date(2026, 12, 12)))
        self.assertAlmostEqual(first_day.projected_spend, 60.0)
        self.assertEqual(ai_daily.aggregate(DAY, mid).projected_spend, 0.0)

    def test_has_activity(self) -> None:
        self.assertFalse(ai_daily.aggregate(DAY, SETTINGS, answer_rows=[answers(ago(1), 5)]).has_activity)
        self.assertFalse(
            ai_daily.aggregate(DAY, SETTINGS, feedback_rows=[dict(day=DAY, helpful=True, n=1)]).has_activity
        )
        self.assertTrue(ai_daily.aggregate(DAY, SETTINGS, opened_rows=[dict(day=DAY, n=1)]).has_activity)
        self.assertTrue(ai_daily.aggregate(DAY, SETTINGS, closed_rows=[dict(day=DAY, closed=1)]).has_activity)
        self.assertTrue(
            ai_daily.aggregate(DAY, SETTINGS, cost_rows=[dict(day=DAY, feature="other", cost=0.01)]).has_activity
        )


class PostDueTests(unittest.TestCase):
    def test_waits_for_ten_past_midnight_utc(self) -> None:
        self.assertIsNone(ai_daily.post_due(datetime(2026, 11, 13, 0, 9, 59, tzinfo=timezone.utc)))
        self.assertEqual(ai_daily.post_due(datetime(2026, 11, 13, 0, 10, tzinfo=timezone.utc)), DAY)
        self.assertEqual(ai_daily.post_due(datetime(2026, 11, 13, 23, 59, tzinfo=timezone.utc)), DAY)

    def test_month_boundary_and_other_timezones(self) -> None:
        self.assertEqual(ai_daily.post_due(datetime(2026, 12, 1, 0, 10, tzinfo=timezone.utc)), date(2026, 11, 30))
        self.assertEqual(ai_daily.post_due(datetime(2027, 1, 1, 5, 0, tzinfo=timezone.utc)), date(2026, 12, 31))
        # 21:30 at UTC-3 is 00:30 UTC of the next day.
        local = datetime(2026, 11, 12, 21, 30, tzinfo=timezone(timedelta(hours=-3)))
        self.assertEqual(ai_daily.post_due(local), DAY)


class ClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_caller_wins(self) -> None:
        pool = SimpleNamespace(fetchval=AsyncMock(side_effect=[DAY, None]))

        self.assertTrue(await ai_daily.claim(DAY, pool=pool))
        self.assertFalse(await ai_daily.claim(DAY, pool=pool))
        sql, arg = pool.fetchval.await_args.args
        self.assertIn("INSERT INTO ai_daily_posts", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertEqual(arg, DAY)

    async def test_release_deletes_the_row(self) -> None:
        pool = SimpleNamespace(execute=AsyncMock())

        await ai_daily.release(DAY, pool=pool)

        sql, arg = pool.execute.await_args.args
        self.assertIn("DELETE FROM ai_daily_posts", sql)
        self.assertEqual(arg, DAY)


class CollectTests(unittest.IsolatedAsyncioTestCase):
    def fake_pool(self):
        async def fetch(sql, *args):
            if "support_ai_traces" in sql:
                return [answers(DAY, 2), answers(DAY, 1, "handoff", "support_escalation")]
            if "GROUP BY model" in sql:
                return [dict(model="claude-haiku-5-5", tokens=900)]
            if "feature" in sql:
                return [dict(day=DAY, feature="support_question", cost=Decimal("0.5"))]
            return []

        async def fetchval(sql, *args):
            if "SUM(cost_usd)" in sql:
                return Decimal("3.5")
            return 2 if "support_info" in sql else 5

        return SimpleNamespace(fetch=AsyncMock(side_effect=fetch), fetchval=AsyncMock(side_effect=fetchval))

    async def test_flushes_and_queries_the_window(self) -> None:
        pool = self.fake_pool()
        now = datetime(2026, 11, 13, 0, 15, tzinfo=timezone.utc)
        with patch.object(ai_daily.ai_usage, "flush", AsyncMock(return_value=0)) as flush:
            data = await ai_daily.collect(DAY, SETTINGS, pool=pool, now=now)

        flush.assert_awaited_once()
        since = datetime(2026, 10, 30, tzinfo=timezone.utc)
        day_start = datetime(2026, 11, 12, tzinfo=timezone.utc)
        until = datetime(2026, 11, 13, tzinfo=timezone.utc)
        calls = {call.args[0]: call.args[1:] for call in pool.fetch.await_args_list}
        self.assertEqual(len(calls), 6)
        for sql, args in calls.items():
            if "GROUP BY model" in sql:
                self.assertEqual(args, (day_start, until))
            else:
                self.assertEqual(args[:2], (since, until), sql)
            if "support_ai_traces" in sql:
                self.assertEqual(args[2], ["support_question", "support_escalation"])
        values = {call.args[0]: call.args[1:] for call in pool.fetchval.await_args_list}
        self.assertEqual(len(values), 3)
        for sql, args in values.items():
            if "SUM(cost_usd)" in sql:
                self.assertEqual(args, (datetime(2026, 11, 1, tzinfo=timezone.utc), until))
            elif "ai_feedback" in sql:
                self.assertEqual(args, (until - timedelta(days=30), until))
            else:
                self.assertEqual(args, ())

        self.assertEqual(data.day, DAY)
        self.assertFalse(data.partial)
        self.assertEqual(data.today.answers_total, 3)
        self.assertEqual(data.today.escalated, 1)
        self.assertEqual(data.today.cost["support"], 0.5)
        self.assertEqual(data.pool_used, {"small": 900, "big": 0})
        self.assertEqual(data.cycle_spend, 3.5)
        self.assertEqual((data.suggestions_waiting, data.answers_unreviewed), (2, 5))

    async def test_days_widens_the_queried_window(self) -> None:
        pool = self.fake_pool()
        now = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
        with patch.object(ai_daily.ai_usage, "flush", AsyncMock(return_value=0)):
            data = await ai_daily.collect(DAY, SETTINGS, pool=pool, now=now, days=30)

        since = datetime(2026, 10, 14, tzinfo=timezone.utc)
        day_start = datetime(2026, 11, 12, tzinfo=timezone.utc)
        until = datetime(2026, 11, 13, tzinfo=timezone.utc)
        for call in pool.fetch.await_args_list:
            sql, *args = call.args
            expected = (day_start, until) if "GROUP BY model" in sql else (since, until)
            self.assertEqual(tuple(args[:2]), expected, sql)
        self.assertEqual(len(data.points), 30)
        self.assertEqual((data.points[0].day, data.points[-1].day), (date(2026, 10, 14), DAY))
        self.assertEqual(data.today.answers_total, 3)
        self.assertTrue(data.partial)

    async def test_partial_when_the_day_is_today(self) -> None:
        now = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
        with patch.object(ai_daily.ai_usage, "flush", AsyncMock(return_value=0)):
            data = await ai_daily.collect(DAY, SETTINGS, pool=self.fake_pool(), now=now)

        self.assertTrue(data.partial)


if __name__ == "__main__":
    unittest.main()
