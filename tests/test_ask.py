import os
import unittest
from collections import defaultdict


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.ask import evaluate_ask_rate_limit, is_ask_channel_allowed
from bulmaai.services.moderation import ModerationState


class AskChannelAllowlistTests(unittest.TestCase):
    def test_empty_allowlist_denies_every_channel(self) -> None:
        self.assertFalse(is_ask_channel_allowed(123, ()))

    def test_allows_channel_present_in_allowlist(self) -> None:
        self.assertTrue(is_ask_channel_allowed(123, (123, 456)))

    def test_denies_channel_missing_from_allowlist(self) -> None:
        self.assertFalse(is_ask_channel_allowed(789, (123, 456)))


class AskRateLimitTests(unittest.TestCase):
    def test_allows_up_to_max_calls_then_denies_the_next(self) -> None:
        state = ModerationState()
        bucket: dict[tuple[int, int], list[float]] = defaultdict(list)
        base = 1000.0

        for offset in range(3):
            events = state.record(bucket, (1, 0), base + offset, 60)
            self.assertIsNone(
                evaluate_ask_rate_limit(events, now=base + offset, window_seconds=60, max_calls=3)
            )

        events = state.record(bucket, (1, 0), base + 3, 60)
        retry_after = evaluate_ask_rate_limit(events, now=base + 3, window_seconds=60, max_calls=3)

        self.assertAlmostEqual(retry_after, 57.0)

    def test_events_outside_the_window_do_not_count(self) -> None:
        state = ModerationState()
        bucket: dict[tuple[int, int], list[float]] = defaultdict(list)
        for offset in range(3):
            state.record(bucket, (1, 0), 1000.0 + offset, 60)

        events = state.record(bucket, (1, 0), 1100.0, 60)

        self.assertIsNone(
            evaluate_ask_rate_limit(events, now=1100.0, window_seconds=60, max_calls=3)
        )

    def test_different_users_are_tracked_independently(self) -> None:
        state = ModerationState()
        bucket: dict[tuple[int, int], list[float]] = defaultdict(list)
        now = 1000.0
        for offset in range(4):
            state.record(bucket, (1, 0), now + offset, 60)

        events = state.record(bucket, (2, 0), now, 60)

        self.assertIsNone(
            evaluate_ask_rate_limit(events, now=now, window_seconds=60, max_calls=3)
        )


if __name__ == "__main__":
    unittest.main()


class AskQuestionMemoryTests(unittest.TestCase):
    def test_remembers_question_by_answer_id_and_stays_bounded(self) -> None:
        from bulmaai.cogs import ai_tickets

        ai_tickets._ASK_QUESTIONS.clear()
        for answer_id in range(ai_tickets._ASK_QUESTIONS_SIZE + 5):
            ai_tickets.remember_ask_question(answer_id, 7, "Bruno", "how do you reach namek?")
        self.assertEqual(len(ai_tickets._ASK_QUESTIONS), ai_tickets._ASK_QUESTIONS_SIZE)
        self.assertNotIn(0, ai_tickets._ASK_QUESTIONS)
        self.assertEqual(ai_tickets._ASK_QUESTIONS[ai_tickets._ASK_QUESTIONS_SIZE + 4][2], "how do you reach namek?")


class CardTextTests(unittest.TestCase):
    def test_reads_card_text_and_drops_footer(self) -> None:
        from types import SimpleNamespace

        from bulmaai.cogs import ai_tickets

        card = {
            "type": 17,
            "components": [
                {"type": 10, "content": f"Use Alt + F.\n{ai_tickets.AI_FOOTER}"},
                {"type": 1, "components": []},
            ],
        }
        message = SimpleNamespace(components=[SimpleNamespace(to_dict=lambda: card)])
        self.assertEqual(ai_tickets._card_text(message), f"Use Alt + F.\n{ai_tickets.AI_FOOTER}".split("\n")[0])
