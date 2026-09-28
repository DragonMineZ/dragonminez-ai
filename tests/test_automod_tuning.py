import io
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from PIL import Image

from bulmaai.services import automod_hits, mod_actions, scam_images
from bulmaai.services.automod_hits import AutomodHit, FilterStats

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def png(pattern: str, *, resize_to=None, fmt="PNG") -> bytes:
    image = Image.new("L", (300, 200))
    for x in range(300):
        for y in range(200):
            value = (x * 255 // 300) if pattern == "gradient" else (255 if (x // 30 + y // 30) % 2 else 0)
            image.putpixel((x, y), value)
    if resize_to:
        image = image.resize(resize_to)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


class DHashTests(unittest.TestCase):
    def test_resized_copy_matches_and_different_image_does_not(self):
        original = scam_images.dhash(png("checker"))
        resized = scam_images.dhash(png("checker", resize_to=(150, 100), fmt="JPEG"))  # smaller and re-encoded
        other = scam_images.dhash(png("gradient"))
        self.assertLessEqual(scam_images.distance(original, resized), 6)
        self.assertGreater(scam_images.distance(original, other), 6)

    def test_garbage_bytes_are_not_an_image(self):
        self.assertIsNone(scam_images.dhash(b"not an image"))

    def test_db_round_trip_of_high_bit_hashes(self):
        value = (1 << 64) - 5
        self.assertLess(scam_images.to_db(value), 0)
        self.assertEqual(scam_images.from_db(scam_images.to_db(value)), value)

    def test_match_uses_distance_threshold(self):
        with patch.dict(scam_images._hashes, {1: 0b1111, 2: 1 << 40}, clear=True):
            self.assertEqual(scam_images.match(0b0111, max_distance=1), 1)
            self.assertIsNone(scam_images.match(0b0111, max_distance=0))

    def test_small_or_non_image_attachments_are_skipped(self):
        image = SimpleNamespace(content_type="image/png", width=640, height=480)
        self.assertTrue(scam_images.is_hashable(image))
        self.assertFalse(scam_images.is_hashable(SimpleNamespace(content_type="image/png", width=48, height=48)))
        self.assertFalse(scam_images.is_hashable(SimpleNamespace(content_type="text/plain", width=None, height=None)))

    def test_thumbnail_url_keeps_signature_params(self):
        url = scam_images._thumbnail_url("https://media.discordapp.net/a/b.png?ex=1&hm=abc&width=10")
        self.assertIn("ex=1", url)
        self.assertIn("hm=abc", url)
        self.assertIn("width=256", url)
        self.assertNotIn("width=10", url)


class SuggestTests(unittest.TestCase):
    settings = SimpleNamespace(moderation_caps_percent=70, moderation_zalgo_enabled=True, moderation_caps_min_length=20)

    def test_noisy_filter_gets_a_loosening_suggestion(self):
        stats = [FilterStats("excessive_caps", hits=10, confirmed=1, false_positives=4)]
        (suggestion,) = automod_hits.suggest(stats, self.settings)
        self.assertEqual((suggestion.setting, suggestion.current, suggestion.suggested), ("moderation_caps_percent", 70, 80))

    def test_quiet_or_rarely_wrong_filters_are_left_alone(self):
        stats = [
            FilterStats("excessive_caps", hits=100, confirmed=0, false_positives=5),  # 5% FP rate
            FilterStats("zalgo", hits=2, confirmed=0, false_positives=2),  # too few samples
        ]
        self.assertEqual(automod_hits.suggest(stats, self.settings), [])

    def test_list_based_filters_get_a_note(self):
        (suggestion,) = automod_hits.suggest([FilterStats("banned_word", 5, 0, 3)], self.settings)
        self.assertIsNone(suggestion.setting)
        self.assertIn("moderation_banned_words", suggestion.note)


def hit(**overrides) -> AutomodHit:
    values = dict(
        id=7, guild_id=1, user_id=5, reason="banned_word", action="delete", details=None, domains=(),
        image_hashes=(), scam_hash_id=None, warn_case_id=None, timed_out=False, alert_message_id=99,
        outcome=None, created_at=NOW,
    )
    values.update(overrides)
    return AutomodHit(**values)


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_false_positive_undoes_warn_timeout_and_scam_hash(self):
        moderator = SimpleNamespace(id=2)
        with (
            patch("bulmaai.services.automod_hits.set_outcome", AsyncMock(return_value=True)),
            patch("bulmaai.services.mod_cases.deactivate_case", AsyncMock(return_value=object())) as deactivate,
            patch("bulmaai.services.mod_actions.perform", AsyncMock()) as perform,
            patch("bulmaai.services.scam_images.remove", AsyncMock(return_value=True)) as remove,
        ):
            undone = await automod_hits.mark_false_positive(
                SimpleNamespace(), SimpleNamespace(id=1), hit(warn_case_id=3, timed_out=True, scam_hash_id=4), moderator
            )
        self.assertEqual(len(undone), 3)
        deactivate.assert_awaited_once_with(1, 3)
        self.assertEqual(perform.await_args.kwargs["action"], "untimeout")
        remove.assert_awaited_once_with(4)

    async def test_false_positive_is_idempotent_and_reports_failed_untimeout(self):
        with patch("bulmaai.services.automod_hits.set_outcome", AsyncMock(return_value=False)):
            self.assertEqual(
                await automod_hits.mark_false_positive(None, SimpleNamespace(id=1), hit(), SimpleNamespace(id=2)),
                ["already marked as a false positive"],
            )
        with (
            patch("bulmaai.services.automod_hits.set_outcome", AsyncMock(return_value=True)),
            patch("bulmaai.services.mod_actions.perform", AsyncMock(side_effect=mod_actions.ModActionError("gone", 404))),
        ):
            undone = await automod_hits.mark_false_positive(None, SimpleNamespace(id=1), hit(timed_out=True), SimpleNamespace(id=2))
        self.assertEqual(undone, ["timeout not removed: gone"])

    async def test_only_an_explicit_learn_adds_scam_images(self):
        add = AsyncMock(return_value=1)
        with (
            patch("bulmaai.services.automod_hits.set_outcome", AsyncMock(side_effect=[True, False])),
            patch("bulmaai.services.scam_images.add", add),
        ):
            self.assertEqual(await automod_hits.mark_confirmed(hit(image_hashes=(11, 12)), 2), 0)  # a ban click
            # Learn still works after an earlier confirmation (outcome already set).
            self.assertEqual(await automod_hits.mark_confirmed(hit(image_hashes=(11, 12)), 2, learn=True), 2)
        self.assertEqual(add.await_count, 2)
        self.assertEqual(add.await_args.kwargs["source"], "learned")

    async def test_update_hit_rejects_unknown_columns(self):
        with self.assertRaises(ValueError):
            await automod_hits.update_hit(1, outcome="confirmed")


if __name__ == "__main__":
    unittest.main()
