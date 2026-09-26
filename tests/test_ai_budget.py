import types
import unittest
from datetime import date
from unittest.mock import patch

from bulmaai.services import ai_budget


def _settings(**limits):
    return types.SimpleNamespace(
        openai_model="gpt-5-mini",
        openai_support_escalation_model="gpt-5",
        openai_support_reasoning_effort="medium",
        **{f"openai_daily_{pool}_token_limit": value for pool, value in limits.items()},
    )


class AIBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        ai_budget.reset()

    def test_classifies_pools_by_model_name_and_tool_use(self) -> None:
        self.assertEqual(ai_budget.pool_for("gpt-5-mini-2025-08-07"), ai_budget.SMALL)
        self.assertEqual(ai_budget.pool_for("gpt-4.1-nano"), ai_budget.SMALL)
        self.assertEqual(ai_budget.pool_for("gpt-5"), ai_budget.BIG)
        self.assertEqual(ai_budget.pool_for("gpt-5-mini", billed=True), ai_budget.BILLED)

    def test_record_ignores_bad_input_and_rolls_over_at_utc_midnight(self) -> None:
        with patch("bulmaai.services.ai_budget._today", return_value=date(2026, 9, 26)):
            ai_budget.record("gpt-5-mini", 100)
            ai_budget.record("gpt-5-mini", None)
            ai_budget.record("gpt-5-mini", "oops")
            ai_budget.record("gpt-5-mini", -5)
            self.assertEqual(ai_budget.used(ai_budget.SMALL), 100)
        with patch("bulmaai.services.ai_budget._today", return_value=date(2026, 9, 27)):
            self.assertEqual(ai_budget.used(ai_budget.SMALL), 0)

    def test_pick_model_downgrades_then_pauses(self) -> None:
        settings = _settings(small=1000, big=100)
        self.assertEqual(ai_budget.pick_model("gpt-5", settings), "gpt-5")
        ai_budget.record("gpt-5", 100)
        self.assertEqual(ai_budget.pick_model("gpt-5", settings), "gpt-5-mini")
        ai_budget.record("gpt-5-mini", 1000)
        self.assertIsNone(ai_budget.pick_model("gpt-5-mini", settings))
        self.assertTrue(ai_budget.is_paused(settings))

    def test_unset_limits_mean_unlimited(self) -> None:
        ai_budget.record("gpt-5-mini", 10**9)
        self.assertFalse(ai_budget.is_paused(types.SimpleNamespace()))

    def test_escalation_route_prefers_billed_tools_then_free_big_then_mini_high(self) -> None:
        settings = _settings(small=1000, big=100, billed=100)
        self.assertEqual(ai_budget.escalation_route(settings), ("gpt-5", "medium", True))
        ai_budget.record("gpt-5", 100, billed=True)
        self.assertEqual(ai_budget.escalation_route(settings), ("gpt-5", "medium", False))
        ai_budget.record("gpt-5", 100)
        self.assertEqual(ai_budget.escalation_route(settings), ("gpt-5-mini", "high", False))
        ai_budget.record("gpt-5-mini", 1000)
        self.assertIsNone(ai_budget.escalation_route(settings))

    def test_seed_restores_todays_usage(self) -> None:
        ai_budget.seed([("gpt-5-mini", False, 50), ("gpt-5", True, 20), ("gpt-5", False, 7)])
        self.assertEqual(
            (ai_budget.used(ai_budget.SMALL), ai_budget.used(ai_budget.BILLED), ai_budget.used(ai_budget.BIG)),
            (50, 20, 7),
        )


if __name__ == "__main__":
    unittest.main()
