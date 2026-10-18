import types
import unittest
from datetime import date
from unittest.mock import patch

from bulmaai.services import ai_budget


def _settings(**limits):
    return types.SimpleNamespace(
        ai_model="claude-haiku-5-5",
        ai_support_escalation_model="claude-opus-5-5",
        ai_support_effort="medium",
        **{f"ai_daily_{pool}_token_limit": value for pool, value in limits.items()},
    )


class AIBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        ai_budget.reset()

    def test_classifies_pools_by_model_name(self) -> None:
        self.assertEqual(ai_budget.pool_for("claude-haiku-5-5"), ai_budget.SMALL)
        self.assertEqual(ai_budget.pool_for("Claude-Haiku-4-5-20251001"), ai_budget.SMALL)
        self.assertEqual(ai_budget.pool_for("claude-opus-5-5"), ai_budget.BIG)
        self.assertEqual(ai_budget.pool_for("claude-sonnet-5"), ai_budget.BIG)
        self.assertEqual(ai_budget.pool_for(None), ai_budget.BIG)

    def test_record_ignores_bad_input_and_rolls_over_at_utc_midnight(self) -> None:
        with patch("bulmaai.services.ai_budget._today", return_value=date(2026, 9, 26)):
            ai_budget.record("claude-haiku-5-5", 100)
            ai_budget.record("claude-haiku-5-5", None)
            ai_budget.record("claude-haiku-5-5", "oops")
            ai_budget.record("claude-haiku-5-5", -5)
            self.assertEqual(ai_budget.used(ai_budget.SMALL), 100)
        with patch("bulmaai.services.ai_budget._today", return_value=date(2026, 9, 27)):
            self.assertEqual(ai_budget.used(ai_budget.SMALL), 0)

    def test_record_response_sums_claude_usage_including_cache(self) -> None:
        usage = types.SimpleNamespace(
            input_tokens=100, output_tokens=50, cache_creation_input_tokens=20, cache_read_input_tokens=None
        )
        ai_budget.record_response(types.SimpleNamespace(model="claude-opus-5-5", usage=usage))
        ai_budget.record_response(types.SimpleNamespace(model="claude-haiku-5-5", usage=None))
        self.assertEqual(ai_budget.used(ai_budget.BIG), 170)
        self.assertEqual(ai_budget.used(ai_budget.SMALL), 0)

    def test_pick_model_downgrades_then_pauses(self) -> None:
        settings = _settings(small=1000, big=100)
        self.assertEqual(ai_budget.pick_model("claude-opus-5-5", settings), "claude-opus-5-5")
        ai_budget.record("claude-opus-5-5", 100)
        self.assertEqual(ai_budget.pick_model("claude-opus-5-5", settings), "claude-haiku-5-5")
        ai_budget.record("claude-haiku-5-5", 1000)
        self.assertIsNone(ai_budget.pick_model("claude-haiku-5-5", settings))
        self.assertTrue(ai_budget.is_paused(settings))

    def test_unset_limits_mean_unlimited(self) -> None:
        ai_budget.record("claude-haiku-5-5", 10**9)
        self.assertFalse(ai_budget.is_paused(types.SimpleNamespace()))

    def test_escalation_route_prefers_big_model_then_small_at_high_effort(self) -> None:
        settings = _settings(small=1000, big=100)
        self.assertEqual(ai_budget.escalation_route(settings), ("claude-opus-5-5", "medium", True))
        ai_budget.record("claude-opus-5-5", 100)
        self.assertEqual(ai_budget.escalation_route(settings), ("claude-haiku-5-5", "high", True))
        ai_budget.record("claude-haiku-5-5", 1000)
        self.assertIsNone(ai_budget.escalation_route(settings))

    def test_seed_restores_todays_usage(self) -> None:
        ai_budget.seed([("claude-haiku-5-5", 50), ("claude-opus-5-5", 20), ("claude-opus-5-5", 7)])
        self.assertEqual((ai_budget.used(ai_budget.SMALL), ai_budget.used(ai_budget.BIG)), (50, 27))


if __name__ == "__main__":
    unittest.main()
