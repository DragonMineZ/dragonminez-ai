import os
import types
import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import ai_budget, ai_usage
from bulmaai.web import routes_usage


def _response(model, **usage):
    return types.SimpleNamespace(model=model, usage=types.SimpleNamespace(**usage))


class CostTests(unittest.TestCase):
    def test_haiku_switches_to_long_prices_over_100k_prompt_tokens(self) -> None:
        self.assertAlmostEqual(ai_usage.cost("claude-haiku-5-5", 50_000, 1_000_000), 0.505)
        self.assertAlmostEqual(ai_usage.cost("claude-haiku-5-5", 100_000, 0), 0.01)
        self.assertAlmostEqual(ai_usage.cost("claude-haiku-5-5", 200_000, 1_000), 0.1025)
        # Cache tokens count toward the prompt size.
        self.assertAlmostEqual(
            ai_usage.cost("claude-haiku-5-5", 1_000, 0, cache_read=100_000),
            1_000 * 0.50 / 1e6 + 100_000 * 0.05 / 1e6,
        )

    def test_sonnet_cache_read_and_write(self) -> None:
        self.assertAlmostEqual(
            ai_usage.cost("claude-sonnet-5-5", 1_000_000, 100_000, cache_read=1_000_000, cache_write=1_000_000),
            2.00 + 1.00 + 0.20 + 2.50,
        )

    def test_searches_and_prefix_match(self) -> None:
        self.assertAlmostEqual(ai_usage.cost("claude-haiku-5-5-20261101", 0, 0, searches=3), 0.03)

    def test_unknown_model_priced_as_most_expensive(self) -> None:
        self.assertEqual(ai_usage.cost("mystery-model", 1_000_000, 1_000_000), ai_usage.cost("claude-sonnet-5-5", 1_000_000, 1_000_000))

    def test_price_table_derives_cache_prices(self) -> None:
        table = ai_usage.price_table()
        self.assertAlmostEqual(table["claude-sonnet-5-5"]["cache_write"], 2.50)
        self.assertAlmostEqual(table["claude-sonnet-5-5"]["cache_read"], 0.20)
        self.assertIsNone(table["claude-sonnet-5-5"]["long_input"])


class CycleTests(unittest.TestCase):
    def test_cycle_day_one(self) -> None:
        self.assertEqual(ai_usage.cycle_bounds(date(2026, 11, 1), 1), (date(2026, 11, 1), date(2026, 12, 1)))
        self.assertEqual(ai_usage.cycle_bounds(date(2026, 11, 30), 1), (date(2026, 11, 1), date(2026, 12, 1)))

    def test_december_to_january(self) -> None:
        self.assertEqual(ai_usage.cycle_bounds(date(2026, 12, 20), 15), (date(2026, 12, 15), date(2027, 1, 15)))
        self.assertEqual(ai_usage.cycle_bounds(date(2027, 1, 3), 15), (date(2026, 12, 15), date(2027, 1, 15)))

    def test_day_28_in_february(self) -> None:
        self.assertEqual(ai_usage.cycle_bounds(date(2027, 2, 27), 28), (date(2027, 1, 28), date(2027, 2, 28)))
        self.assertEqual(ai_usage.cycle_bounds(date(2027, 2, 28), 28), (date(2027, 2, 28), date(2027, 3, 28)))

    def test_out_of_range_day_is_clamped(self) -> None:
        self.assertEqual(ai_usage.cycle_bounds(date(2027, 3, 30), 31), (date(2027, 3, 28), date(2027, 4, 28)))
        self.assertEqual(ai_usage.cycle_bounds(date(2027, 3, 30), "x"), (date(2027, 3, 1), date(2027, 4, 1)))

    def test_range_and_day_params(self) -> None:
        today = date(2026, 11, 9)
        self.assertEqual(routes_usage.range_start("cycle", today, date(2026, 11, 1)), date(2026, 11, 1))
        self.assertEqual(routes_usage.range_start("7", today, date(2026, 11, 1)), date(2026, 11, 3))
        with self.assertRaises(ValueError):
            routes_usage.range_start("14", today, date(2026, 11, 1))
        self.assertEqual(routes_usage.parse_day("2026-11-09", today), today)
        for bad in ("2026-11-10", "2025-01-01", "2026-1-1", "nope", ""):
            with self.assertRaises(ValueError):
                routes_usage.parse_day(bad, today)


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        ai_budget.reset()
        ai_usage.reset()

    def tearDown(self) -> None:
        ai_usage.reset()

    def test_record_response_without_loop_or_db_updates_pools_and_queues_the_feature(self) -> None:
        response = _response(
            "claude-haiku-5-5",
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=2,
            server_tool_use={"web_search_requests": 2},
        )
        ai_budget.record_response(response, "other")
        self.assertEqual(ai_budget.used(ai_budget.SMALL), 20)
        [row] = ai_usage.pending()
        self.assertEqual(row[1:8], ("other", "claude-haiku-5-5", 10, 5, 3, 2, 2))
        self.assertAlmostEqual(row[8], ai_usage.cost("claude-haiku-5-5", 10, 5, 3, 2, 2))

    def test_default_feature_and_bad_usage_never_raise(self) -> None:
        ai_budget.record_response(_response("claude-sonnet-5-5", input_tokens="x", output_tokens=None))
        ai_budget.record_response(types.SimpleNamespace(model=None, usage=None))
        ai_budget.record_response(object())
        [row] = ai_usage.pending()
        self.assertEqual(row[1], "other")
        self.assertEqual(row[3:8], (0, 0, 0, 0, 0))

    def test_queue_is_capped(self) -> None:
        for _ in range(ai_usage.MAX_QUEUE + 10):
            ai_usage.record("other", "claude-haiku-5-5", 1)
        self.assertEqual(len(ai_usage.pending()), ai_usage.MAX_QUEUE)

    def test_every_caller_label_has_a_human_name(self) -> None:
        for label in ai_usage.FEATURE_LABELS:
            self.assertRegex(label, r"^[a-z_]+$")


class FlushTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ai_usage.reset()

    def tearDown(self) -> None:
        ai_usage.reset()

    async def test_flush_writes_queued_rows_once(self) -> None:
        pool = types.SimpleNamespace(executemany=AsyncMock())
        ai_usage.record("code_notes", "claude-haiku-5-5", 1, 2)
        self.assertEqual(await ai_usage.flush(pool), 1)
        rows = pool.executemany.await_args.args[1]
        self.assertEqual(rows[0][1], "code_notes")
        self.assertFalse(rows[0][-1])
        self.assertEqual(await ai_usage.flush(pool), 0)

    async def test_flush_drops_rows_when_the_db_fails(self) -> None:
        pool = types.SimpleNamespace(executemany=AsyncMock(side_effect=OSError("db down")))
        ai_usage.record("code_notes", "claude-haiku-5-5", 1, 2)
        with self.assertLogs("bulmaai.services.ai_usage", level="INFO"):
            self.assertEqual(await ai_usage.flush(pool), 0)
        self.assertEqual(ai_usage.pending(), [])


class BudgetResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_budget_reset_reads_the_single_row(self) -> None:
        at = datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc)
        pool = types.SimpleNamespace(fetchval=AsyncMock(return_value=at))
        self.assertEqual(await ai_usage.last_budget_reset(pool), at)
        self.assertIn("ai_budget_reset", pool.fetchval.await_args.args[0])
        pool = types.SimpleNamespace(fetchval=AsyncMock(return_value=None))
        self.assertIsNone(await ai_usage.last_budget_reset(pool))

    async def test_mark_budget_reset_upserts_row_one(self) -> None:
        at = datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc)
        pool = types.SimpleNamespace(execute=AsyncMock())
        await ai_usage.mark_budget_reset(pool, at)
        sql, value = pool.execute.await_args.args
        self.assertIn("ON CONFLICT (id) DO UPDATE", sql)
        self.assertEqual(value, at)


if __name__ == "__main__":
    unittest.main()
