import random
import unittest
from datetime import datetime, timedelta, timezone

from bulmaai.cogs.presence import LINES, MAX_STATUS, Deck, needed, render, uptime_text

FULL = {
    "members": 100_000, "online": 12_345, "boosts": 14, "ai_today": 321, "tickets_total": 4567,
    "tickets_open": 9, "bugs_week": 17, "warns_week": 5, "automod_today": 42, "top_level": 88,
    "jar_downloads": 1500, "uptime": "3d 4h", "bans_total": 900, "bans_next": 901,
}


class PresenceTests(unittest.TestCase):
    def test_every_line_renders_with_all_stats_and_fits(self):
        for line in LINES:
            text = render(line, FULL)
            self.assertIsNotNone(text, line)
            self.assertLessEqual(len(text), MAX_STATUS)
            self.assertNotIn("@", text)  # no ping-looking text in the status

    def test_numbers_get_thousands_separators(self):
        self.assertEqual(render("Babysitting {members} Saiyans", FULL), "Babysitting 100,000 Saiyans")

    def test_missing_or_zero_stat_skips_the_line(self):
        self.assertIsNone(render("{tickets_open} open", {"tickets_open": 0}))
        self.assertIsNone(render("{members} here", {}))

    def test_deck_shows_every_usable_line_once_before_repeating(self):
        deck = Deck(rng=random.Random(1))
        static = [line for line in LINES if not needed(line)]
        seen = [deck.next({}) for _ in static]
        self.assertCountEqual(seen, static)

    def test_deck_falls_back_when_nothing_renders(self):
        self.assertEqual(Deck(["{members}"]).next({}), "Doing Bulma things")

    def test_uptime_text(self):
        now = datetime.now(timezone.utc)
        self.assertIsNone(uptime_text(now, now))
        self.assertEqual(uptime_text(now - timedelta(minutes=75), now), "1h 15m")
        self.assertEqual(uptime_text(now - timedelta(days=2, hours=5), now), "2d 5h")


if __name__ == "__main__":
    unittest.main()
