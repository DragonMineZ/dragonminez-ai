import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# cogs.moderation imports services.mod_actions, whose import chain (mod_cases -> database.db)
# calls load_settings() at import time; these need to exist regardless of test run/discovery order.
os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.moderation import _HUMAN_SHAPED_REASONS, _WARN_REASONS, ModerationCog, _Incident
from bulmaai.services.mod_actions import ActionResult
from bulmaai.services.moderation import ImagePost, ModerationAction, ModerationDecision
from bulmaai.ui.mod_views import QUICK


class Role(int):
    pass


class FakeAttachment:
    def __init__(
        self,
        *,
        filename: str = "scam.png",
        content_type: str = "image/png",
        width: int = 800,
        height: int = 600,
        url: str = "https://cdn.discordapp.com/scam.png",
        proxy_url: str = "https://media.discordapp.net/scam.png",
        size: int = 1024,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.width = width
        self.height = height
        self.url = url
        self.proxy_url = proxy_url
        self.size = size


class FakeLogMessage:
    def __init__(self, message_id: int = 8888) -> None:
        self.id = message_id
        self.embeds: list = []
        self.edit_calls: list = []

    async def edit(self, **kwargs) -> None:
        self.edit_calls.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]


class FakeLogChannel:
    def __init__(self) -> None:
        self.id = 500
        self.sent: list = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        message = FakeLogMessage()
        if "embed" in kwargs:
            message.embeds = [kwargs["embed"]]
        return message


def _guild_and_author(*, author_id: int = 3, guild_id: int = 1):
    me = SimpleNamespace(guild_permissions=SimpleNamespace(moderate_members=True), top_role=Role(5))
    guild = SimpleNamespace(id=guild_id, me=me, owner_id=999, get_channel=lambda _cid: None)

    class Author:
        id = author_id
        mention = f"<@{author_id}>"
        top_role = Role(1)
        guild_permissions = SimpleNamespace(administrator=False)

        async def timeout_for(self, duration, *, reason=None):
            self.timeouts.append((duration, reason))

    author = Author()
    author.guild = guild
    author.timeouts = []
    return guild, author


def _make_message(message_id: int, guild, author, *, channel_id: int = 10, attachments=None):
    return SimpleNamespace(
        id=message_id,
        guild=guild,
        channel=SimpleNamespace(id=channel_id, send=AsyncMock()),
        author=author,
        jump_url=f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}",
        attachments=attachments or [],
        delete=AsyncMock(),
    )


DEFAULT_SETTINGS = dict(
    moderation_image_burst_timeout_seconds=7 * 24 * 3600,
    moderation_image_burst_purge_seconds=600,
    moderation_image_burst_window_seconds=20,
    moderation_image_burst_count=3,
    moderation_log_channel_id=None,
    discord_log_channel_id=None,
    moderation_scam_images_enabled=True,
    moderation_disabled_filters=(),
    moderation_scam_images_enforce=False,
    moderation_scam_image_distance=6,
)


def _make_cog(*, log_channel: "FakeLogChannel | None" = None, **overrides) -> ModerationCog:
    cog = ModerationCog.__new__(ModerationCog)
    settings_kwargs = dict(DEFAULT_SETTINGS)
    if log_channel is not None:
        settings_kwargs["moderation_log_channel_id"] = 999
    settings_kwargs.update(overrides)
    settings = SimpleNamespace(**settings_kwargs)
    cog.bot = SimpleNamespace(settings=settings, get_channel=lambda _cid: log_channel)
    cog._recent_message_channels = {}
    cog._incidents = {}
    cog._recent_images = {}
    cog._pending_image_bursts = {}
    cog._record_case = AsyncMock()  # not under test here; automod_hits.* is what these tests cover
    return cog


class HitRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_incident_records_one_hit_and_later_stores_alert_message_id(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author)
        log_channel = FakeLogChannel()
        cog = _make_cog(log_channel=log_channel)
        decision = ModerationDecision(
            action=ModerationAction.DELETE, reason="blocked_domain", domains=("evil.example",)
        )

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=42)) as record_hit,
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()) as update_hit,
            patch("bulmaai.services.mod_actions.perform", AsyncMock()),
        ):
            await cog._apply_decision(message, decision)

        record_hit.assert_awaited_once()
        kwargs = record_hit.call_args.kwargs
        self.assertEqual(kwargs["guild_id"], guild.id)
        self.assertEqual(kwargs["user_id"], author.id)
        self.assertEqual(kwargs["reason"], "blocked_domain")
        self.assertEqual(kwargs["action"], "delete")
        self.assertEqual(kwargs["domains"], ("evil.example",))
        incident = cog._incidents[(guild.id, author.id)]
        self.assertEqual(incident.hit_id, 42)
        update_hit.assert_any_call(42, alert_message_id=8888)

    async def test_second_message_in_the_same_incident_does_not_record_another_hit(self) -> None:
        guild, author = _guild_and_author()
        cog = _make_cog()
        decision = ModerationDecision(action=ModerationAction.DELETE, reason="excessive_caps")

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=42)) as record_hit,
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()),
        ):
            await cog._apply_decision(_make_message(1, guild, author), decision)
            await cog._apply_decision(_make_message(2, guild, author), decision)

        record_hit.assert_awaited_once()
        incident = cog._incidents[(guild.id, author.id)]
        self.assertEqual(incident.hits, 2)
        self.assertEqual(incident.hit_id, 42)


class DbFailureResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_hit_failure_does_not_stop_the_delete(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author)
        cog = _make_cog()
        decision = ModerationDecision(action=ModerationAction.DELETE, reason="excessive_caps")

        with patch(
            "bulmaai.services.automod_hits.record_hit", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            await cog._apply_decision(message, decision)

        message.delete.assert_awaited_once()
        incident = cog._incidents[(guild.id, author.id)]
        self.assertIsNone(incident.hit_id)
        self.assertEqual(incident.deleted, 1)


class WarnStrikeHitTests(unittest.IsolatedAsyncioTestCase):
    async def test_warn_strike_stores_warn_case_id(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author)
        cog = _make_cog()
        decision = ModerationDecision(action=ModerationAction.DELETE, reason="banned_word", details="matched 'x'")
        result = ActionResult(action="warn", case_id=55)

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=9)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()) as update_hit,
            patch("bulmaai.services.mod_actions.perform", AsyncMock(return_value=result)),
        ):
            await cog._apply_decision(message, decision)

        update_hit.assert_any_call(9, warn_case_id=55)
        incident = cog._incidents[(guild.id, author.id)]
        self.assertFalse(incident.timed_out)

    async def test_warn_strike_escalation_timeout_marks_the_hit_timed_out(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author)
        cog = _make_cog()
        decision = ModerationDecision(action=ModerationAction.DELETE, reason="banned_word")
        escalation = ActionResult(action="timeout", case_id=56)
        result = ActionResult(action="warn", case_id=55, escalation=escalation)

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=9)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()) as update_hit,
            patch("bulmaai.services.mod_actions.perform", AsyncMock(return_value=result)),
        ):
            await cog._apply_decision(message, decision)

        update_hit.assert_any_call(9, warn_case_id=55, timed_out=True)
        incident = cog._incidents[(guild.id, author.id)]
        # A false positive later must be able to undo this escalation too.
        self.assertTrue(incident.timed_out)


class AutomodTimeoutHitTests(unittest.IsolatedAsyncioTestCase):
    async def test_automod_timeout_marks_the_hit_timed_out(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author)
        cog = _make_cog()
        decision = ModerationDecision(action=ModerationAction.TIMEOUT, reason="link burst", details="5 events in 60s")

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=9)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()) as update_hit,
        ):
            await cog._apply_decision(message, decision)

        self.assertTrue(author.timeouts)
        update_hit.assert_any_call(9, timed_out=True, action="timeout")
        incident = cog._incidents[(guild.id, author.id)]
        self.assertTrue(incident.timed_out)


class ScamImageEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_skipped_entirely_when_the_list_is_empty(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author, attachments=[FakeAttachment()])
        cog = _make_cog()

        with (
            patch("bulmaai.services.scam_images.is_empty", return_value=True),
            patch("bulmaai.services.scam_images.hash_attachment", AsyncMock()) as hash_attachment,
        ):
            result = await cog._evaluate_scam_images(message)

        self.assertIsNone(result)
        hash_attachment.assert_not_awaited()

    async def test_shadow_mode_match_alerts_without_deleting(self) -> None:
        guild, author = _guild_and_author()
        attachment = FakeAttachment()
        message = _make_message(1, guild, author, attachments=[attachment])
        cog = _make_cog(moderation_scam_images_enforce=False)

        with (
            patch("bulmaai.services.scam_images.is_empty", return_value=False),
            patch("bulmaai.services.scam_images.is_hashable", return_value=True),
            patch("bulmaai.services.scam_images.hash_attachment", AsyncMock(return_value=0)),
            patch.dict("bulmaai.services.scam_images._hashes", {7: 0}, clear=True),
            patch("bulmaai.services.scam_images.note_hit", AsyncMock()) as note_hit,
        ):
            result = await cog._evaluate_scam_images(message)

        self.assertIsNotNone(result)
        decision, value = result
        self.assertEqual(value, 0)
        self.assertEqual(decision.action, ModerationAction.ALERT)
        self.assertEqual(decision.reason, "scam_image")
        self.assertEqual(decision.scam_hash_id, 7)
        note_hit.assert_awaited_once_with(7)

    async def test_enforce_mode_match_deletes(self) -> None:
        guild, author = _guild_and_author()
        attachment = FakeAttachment()
        message = _make_message(1, guild, author, attachments=[attachment])
        cog = _make_cog(moderation_scam_images_enforce=True)

        with (
            patch("bulmaai.services.scam_images.is_empty", return_value=False),
            patch("bulmaai.services.scam_images.is_hashable", return_value=True),
            patch("bulmaai.services.scam_images.hash_attachment", AsyncMock(return_value=0)),
            patch.dict("bulmaai.services.scam_images._hashes", {7: 0}, clear=True),
            patch("bulmaai.services.scam_images.note_hit", AsyncMock()),
        ):
            result = await cog._evaluate_scam_images(message)

        self.assertIsNotNone(result)
        decision, _value = result
        self.assertEqual(decision.action, ModerationAction.DELETE)
        self.assertEqual(decision.reason, "scam_image")


class ScamCheckPrecedenceTests(unittest.TestCase):
    def test_shadow_mode_never_replaces_an_image_burst(self) -> None:
        from bulmaai.cogs.moderation import IMAGE_BURST_REASON, scam_check_applies

        burst = ModerationDecision(action=ModerationAction.TIMEOUT, reason=IMAGE_BURST_REASON)
        self.assertFalse(scam_check_applies(burst, enforcing=False))
        self.assertTrue(scam_check_applies(burst, enforcing=True))
        self.assertTrue(scam_check_applies(ModerationDecision.allow(), enforcing=False))
        link = ModerationDecision(action=ModerationAction.DELETE, reason="blocked_domain")
        self.assertFalse(scam_check_applies(link, enforcing=True))


class ScamImageReasonClassificationTests(unittest.TestCase):
    def test_scam_image_is_not_human_shaped_or_a_warn_reason(self) -> None:
        # scam_image is spam-shaped: it must keep the long escalation timeout and never trigger
        # a warn strike, unlike the human-authored filters.
        self.assertNotIn("scam_image", _HUMAN_SHAPED_REASONS)
        self.assertNotIn("scam_image", _WARN_REASONS)


class ImageBurstHashStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_burst_stores_image_hashes_on_the_hit(self) -> None:
        from bulmaai.cogs import moderation as moderation_cog

        self.addCleanup(
            setattr, moderation_cog, "IMAGE_BURST_CONFIRM_SECONDS", moderation_cog.IMAGE_BURST_CONFIRM_SECONDS
        )
        moderation_cog.IMAGE_BURST_CONFIRM_SECONDS = 0.01

        guild, author = _guild_and_author()
        attachment1 = FakeAttachment(filename="a.png")
        attachment2 = FakeAttachment(filename="b.png")
        message1 = _make_message(1, guild, author, channel_id=10, attachments=[attachment1])
        message2 = _make_message(2, guild, author, channel_id=11, attachments=[attachment2])

        cog = _make_cog(log_channel=FakeLogChannel())
        now = time.monotonic()
        cog._recent_images = {
            (guild.id, author.id): {
                0: ImagePost(posted_at=now, channel_id=10, signatures=((1, 1, 1),)),
                1: ImagePost(posted_at=now, channel_id=11, signatures=((2, 2, 2),)),
            }
        }
        decision = ModerationDecision(action=ModerationAction.TIMEOUT, reason="image burst", details="3 images")

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=77)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()) as update_hit,
            patch("bulmaai.services.scam_images.is_hashable", return_value=True),
            patch("bulmaai.services.scam_images.hash_attachment", AsyncMock(side_effect=[111, 222])),
            patch("bulmaai.services.scam_images.fetch_preview", AsyncMock(return_value=b"fake-bytes")),
        ):
            await asyncio.gather(
                cog._confirm_then_apply_image_burst(message1, decision),
                cog._confirm_then_apply_image_burst(message2, decision),
            )

        hash_updates = [call for call in update_hit.await_args_list if "image_hashes" in call.kwargs]
        self.assertTrue(hash_updates, "expected an update_hit(image_hashes=...) call")
        self.assertEqual(hash_updates[0].args[0], 77)
        self.assertEqual(set(hash_updates[0].kwargs["image_hashes"]), {111, 222})
        incident = cog._incidents[(guild.id, author.id)]
        self.assertEqual(set(incident.image_hashes), {111, 222})


class QuickActionsViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_image_alert_uses_falsepos_not_dismiss(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author)
        incident = _Incident(
            decision=ModerationDecision(action=ModerationAction.DELETE, reason="blocked_domain"),
            first_message=message,
        )

        view = ModerationCog._quick_actions_view_for(incident)

        self.assertEqual(
            [child.custom_id for child in view.children],
            [f"{QUICK}:timeout:{author.id}", f"{QUICK}:ban:{author.id}", f"{QUICK}:falsepos:{author.id}"],
        )

    async def test_timed_out_incident_offers_untimeout(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author)
        incident = _Incident(
            decision=ModerationDecision(action=ModerationAction.TIMEOUT, reason="link burst"),
            first_message=message,
            timed_out=True,
        )

        view = ModerationCog._quick_actions_view_for(incident)

        self.assertEqual(
            [child.custom_id for child in view.children],
            [f"{QUICK}:untimeout:{author.id}", f"{QUICK}:ban:{author.id}", f"{QUICK}:falsepos:{author.id}"],
        )

    async def test_image_alert_offers_learn_with_the_flagged_message_as_its_target(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(5, guild, author, channel_id=20, attachments=[FakeAttachment()])
        incident = _Incident(
            decision=ModerationDecision(action=ModerationAction.DELETE, reason="scam_image"),
            first_message=message,
        )

        view = ModerationCog._quick_actions_view_for(incident)

        self.assertEqual(
            [child.custom_id for child in view.children],
            [
                f"{QUICK}:timeout:{author.id}",
                f"{QUICK}:ban:{author.id}",
                f"{QUICK}:learn:{author.id}:20:5",
                f"{QUICK}:falsepos:{author.id}",
            ],
        )


class ImageAlertPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_log_uploads_a_preview_and_points_the_embed_at_it(self) -> None:
        guild, author = _guild_and_author()
        attachment = FakeAttachment(filename="scam.png")
        message = _make_message(1, guild, author, channel_id=10, attachments=[attachment])
        log_channel = FakeLogChannel()
        cog = _make_cog(log_channel=log_channel)
        decision = ModerationDecision(
            action=ModerationAction.DELETE,
            reason="scam_image",
            details="matches known scam image #7 (2 bits apart)",
        )

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=1)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()),
            patch("bulmaai.services.scam_images.fetch_preview", AsyncMock(return_value=b"bytes")) as fetch_preview,
        ):
            await cog._apply_decision(message, decision)

        fetch_preview.assert_awaited_once_with(attachment)
        self.assertEqual(len(log_channel.sent), 1)
        sent_kwargs = log_channel.sent[0]
        self.assertIsInstance(sent_kwargs["file"], discord.File)
        self.assertEqual(sent_kwargs["embed"].image.url, "attachment://scam.png")
        custom_ids = [child.custom_id for child in sent_kwargs["view"].children]
        self.assertIn(f"{QUICK}:learn:{author.id}:10:1", custom_ids)
        self.assertIn(f"{QUICK}:falsepos:{author.id}", custom_ids)

    async def test_failed_preview_fetch_still_sends_the_alert(self) -> None:
        guild, author = _guild_and_author()
        message = _make_message(1, guild, author, attachments=[FakeAttachment()])
        log_channel = FakeLogChannel()
        cog = _make_cog(log_channel=log_channel)
        decision = ModerationDecision(action=ModerationAction.DELETE, reason="scam_image")

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=1)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()),
            patch("bulmaai.services.scam_images.fetch_preview", AsyncMock(return_value=None)),
        ):
            await cog._apply_decision(message, decision)

        self.assertEqual(len(log_channel.sent), 1)
        self.assertNotIn("file", log_channel.sent[0])

    async def test_debounced_update_carries_the_image_url_forward(self) -> None:
        from bulmaai.cogs import moderation as moderation_cog

        self.addCleanup(
            setattr, moderation_cog, "LOG_UPDATE_DEBOUNCE_SECONDS", moderation_cog.LOG_UPDATE_DEBOUNCE_SECONDS
        )
        moderation_cog.LOG_UPDATE_DEBOUNCE_SECONDS = 0

        guild, author = _guild_and_author()
        attachment = FakeAttachment(filename="scam.png")
        message = _make_message(1, guild, author, attachments=[attachment])
        log_channel = FakeLogChannel()
        cog = _make_cog(log_channel=log_channel)
        decision = ModerationDecision(action=ModerationAction.DELETE, reason="scam_image")

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=1)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()),
            patch("bulmaai.services.scam_images.fetch_preview", AsyncMock(return_value=b"bytes")),
        ):
            await cog._apply_decision(message, decision)

        incident = cog._incidents[(guild.id, author.id)]
        original_image_url = incident.log_message.embeds[0].image.url
        self.assertEqual(original_image_url, "attachment://scam.png")

        incident.revision = 1  # pretend a follow-up hit landed, the way _schedule_log_update would
        await cog._flush_log_update(incident)

        self.assertEqual(len(incident.log_message.edit_calls), 1)
        edit_kwargs = incident.log_message.edit_calls[0]
        self.assertNotIn("file", edit_kwargs)
        self.assertNotIn("attachments", edit_kwargs)
        self.assertEqual(edit_kwargs["embed"].image.url, original_image_url)


if __name__ == "__main__":
    unittest.main()
