import os
import re
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# cogs.moderation now imports services.mod_actions, whose import chain (mod_cases -> database.db)
# calls load_settings() at import time; these need to exist regardless of test run/discovery order.
os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.moderation import _NOTICE_TEXT, _READABLE_REASONS, ModerationCog
from bulmaai.services.moderation import (
    AttachmentMetadata,
    DomainClassification,
    ModerationAction,
    ModerationDecision,
    classify_domain,
    defang_domain,
    decide_burst_threshold,
    detect_discord_invites,
    extract_image_attachments,
    extract_urls,
)


class AutomodTextMentionTests(unittest.TestCase):
    def test_bot_written_automod_text_never_contains_a_live_everyone_or_here(self) -> None:
        # The everyone_ping notice once said "@everyone/@here" bare and pinged the whole server.
        for text in (*_NOTICE_TEXT.values(), *_READABLE_REASONS.values()):
            self.assertIsNone(re.search(r"(?<!`)@(everyone|here)", text), text)


class ModerationUrlTests(unittest.TestCase):
    def test_extract_urls_normalizes_defanged_links(self) -> None:
        urls = extract_urls("Check hxxps://Evil[.]Example/path and good.com\u200b/query")

        self.assertEqual(
            [url.normalized for url in urls],
            ["https://evil.example/path", "good.com/query"],
        )
        self.assertEqual([url.domain for url in urls], ["evil.example", "good.com"])

    def test_classify_domain_prefers_blocked_over_allowed_parent(self) -> None:
        result = classify_domain(
            "cdn.bad.example",
            allowed_domains=("example",),
            blocked_domains=("bad.example",),
        )

        self.assertEqual(result, DomainClassification.BLOCKED)

    def test_defang_domain_replaces_dots_for_log_safety(self) -> None:
        self.assertEqual(defang_domain("Sub.Bad.Example."), "sub[.]bad[.]example")


class ModerationDiscordInviteTests(unittest.TestCase):
    def test_detect_discord_invites_handles_defanged_and_zero_width_text(self) -> None:
        invites = detect_discord_invites("join hxxp://disco\u200brd[.]gg/AbC-123 today")

        self.assertEqual(len(invites), 1)
        self.assertEqual(invites[0].code, "AbC-123")
        self.assertEqual(invites[0].domain, "discord.gg")

    def test_detect_discord_invites_ignores_non_invite_discord_urls(self) -> None:
        invites = detect_discord_invites("https://discord.com/channels/1/2")

        self.assertEqual(invites, ())


class ModerationAttachmentTests(unittest.TestCase):
    def test_extract_image_attachments_uses_duck_typed_attachment_fields(self) -> None:
        class Attachment:
            filename = "proof.PNG"
            content_type = None
            url = "https://cdn.example/proof.PNG"
            size = 512
            width = 640
            height = 480

        images = extract_image_attachments([Attachment()])

        self.assertEqual(
            images,
            (
                AttachmentMetadata(
                    filename="proof.PNG",
                    content_type=None,
                    url="https://cdn.example/proof.PNG",
                    size=512,
                    width=640,
                    height=480,
                    extension=".png",
                    is_image=True,
                ),
            ),
        )


class ModerationDecisionTests(unittest.TestCase):
    def test_burst_threshold_flags_when_recent_events_reach_threshold(self) -> None:
        decision = decide_burst_threshold(
            event_times=(91.0, 94.5, 100.0),
            now=100.0,
            window_seconds=10.0,
            max_events=3,
            action=ModerationAction.TIMEOUT,
        )

        self.assertEqual(decision.action, ModerationAction.TIMEOUT)
        self.assertEqual(decision.reason, "burst_threshold")
        self.assertIn("3 events in 10s", decision.details)

    def test_burst_threshold_allows_when_under_threshold(self) -> None:
        decision = decide_burst_threshold(
            event_times=(89.0, 100.0),
            now=100.0,
            window_seconds=10.0,
            max_events=3,
        )

        self.assertEqual(decision, ModerationDecision.allow("burst_threshold_not_met"))


class ModerationTimeoutEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_decision_times_out_and_purges_recent_channels(self) -> None:
        purge_calls: list[dict] = []
        timeout_calls: list[tuple[timedelta, str | None]] = []
        deleted: list[str] = []

        class FakeChannel:
            def __init__(self, channel_id: int) -> None:
                self.id = channel_id

            async def purge(self, *, limit, after, check, reason):
                purge_calls.append(
                    {"channel_id": self.id, "limit": limit, "after": after, "reason": reason}
                )
                fake_message = SimpleNamespace(author=SimpleNamespace(id=3))
                self_match = check(fake_message)
                other_match = check(SimpleNamespace(author=SimpleNamespace(id=99)))
                return [fake_message] if self_match and not other_match else []

        spam_channel = FakeChannel(2)
        other_channel = FakeChannel(7)
        guild = SimpleNamespace(
            id=1,
            get_channel=lambda channel_id: {2: spam_channel, 7: other_channel}.get(channel_id),
        )

        class FakeAuthor:
            id = 3

            async def timeout_for(self, duration, *, reason=None):
                timeout_calls.append((duration, reason))

        class FakeMessage:
            id = 1234
            guild = None
            channel = spam_channel
            author = FakeAuthor()

            async def delete(self, *, reason=None):
                deleted.append(reason)

        message = FakeMessage()
        message.guild = guild

        cog = ModerationCog.__new__(ModerationCog)
        cog.bot = SimpleNamespace(
            settings=SimpleNamespace(
                moderation_image_burst_timeout_seconds=7 * 24 * 3600,
                moderation_image_burst_purge_seconds=600,
                moderation_log_channel_id=None,
                discord_log_channel_id=None,
            )
        )
        cog._recent_message_channels = {(1, 3): {7: time.monotonic()}}
        cog._incidents = {}

        decision = ModerationDecision(
            action=ModerationAction.TIMEOUT,
            reason="image burst",
            details="4 images across 2 messages in 20s",
            image_count=2,
        )

        with (
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=1)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()),
        ):
            await cog._apply_decision(message, decision)

        self.assertEqual(deleted, ["BulmaAI moderation: image burst"])
        self.assertEqual(len(timeout_calls), 1)
        self.assertEqual(timeout_calls[0][0], timedelta(days=7))
        purged_channel_ids = {call["channel_id"] for call in purge_calls}
        self.assertEqual(purged_channel_ids, {2, 7})
        self.assertNotIn((1, 3), cog._recent_message_channels)


class ModerationIncidentTests(unittest.IsolatedAsyncioTestCase):
    """A spam wave must produce one alert and one timeout attempt, not one per message."""

    def _setup(self, *, member_outranks_bot: bool):
        self.enterContext(patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=1)))
        self.enterContext(patch("bulmaai.services.automod_hits.update_hit", AsyncMock()))
        self.sent: list = []
        self.edits: list = []
        self.timeouts: list = []
        self.deleted: list[int] = []
        self.cases: list[str] = []
        test = self

        class LogMessage:
            id = 9000
            embeds: list = []

            async def edit(self, *, embed, view=None):
                test.edits.append(embed)

        class LogChannel:
            id = 500

            async def send(self, *, embed, view=None, allowed_mentions):
                test.sent.append(embed)
                return LogMessage()

        class Role(int):
            pass

        me = SimpleNamespace(guild_permissions=SimpleNamespace(moderate_members=True), top_role=Role(5))
        guild = SimpleNamespace(id=1, me=me, owner_id=999, get_channel=lambda _cid: None)

        class Author:
            id = 3
            top_role = Role(9 if member_outranks_bot else 1)

            async def timeout_for(self, duration, *, reason=None):
                test.timeouts.append(duration)

        author = Author()
        author.guild = guild

        def make_message(message_id: int, channel_id: int):
            async def delete(*, reason=None):
                test.deleted.append(message_id)

            return SimpleNamespace(
                id=message_id,
                guild=guild,
                channel=SimpleNamespace(id=channel_id),
                author=author,
                jump_url=f"https://discord.com/channels/1/{channel_id}/{message_id}",
                attachments=[],
                delete=delete,
            )

        cog = ModerationCog.__new__(ModerationCog)
        cog.bot = SimpleNamespace(
            settings=SimpleNamespace(
                moderation_image_burst_timeout_seconds=7 * 24 * 3600,
                moderation_image_burst_purge_seconds=600,
            )
        )
        cog._recent_message_channels = {}
        cog._incidents = {}

        async def resolve_log_channel():
            return LogChannel()

        async def record_case(message, decision, *, timed_out):
            test.cases.append(decision.action.value)

        cog._resolve_log_channel = resolve_log_channel
        cog._record_case = record_case
        return cog, make_message

    async def test_concurrent_burst_messages_share_one_incident(self) -> None:
        import asyncio

        from bulmaai.cogs import moderation as moderation_cog

        self.addCleanup(setattr, moderation_cog, "LOG_UPDATE_DEBOUNCE_SECONDS", moderation_cog.LOG_UPDATE_DEBOUNCE_SECONDS)
        moderation_cog.LOG_UPDATE_DEBOUNCE_SECONDS = 0
        cog, make_message = self._setup(member_outranks_bot=False)
        decision = ModerationDecision(action=ModerationAction.TIMEOUT, reason="link burst", details="5 events in 60s")

        await asyncio.gather(
            *(cog._apply_decision(make_message(i, 10 + i), decision) for i in range(4))
        )
        await asyncio.sleep(0.05)

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.timeouts), 1)
        self.assertEqual(self.cases, ["timeout"])
        self.assertEqual(sorted(self.deleted), [0, 1, 2, 3])
        incident = cog._incidents[(1, 3)]
        self.assertEqual((incident.hits, incident.deleted, len(incident.channel_ids)), (4, 4, 4))
        self.assertTrue(self.edits, "follow-ups should update the original alert")

    async def test_timeout_skipped_without_api_call_when_member_outranks_bot(self) -> None:
        cog, make_message = self._setup(member_outranks_bot=True)
        decision = ModerationDecision(action=ModerationAction.TIMEOUT, reason="image burst")

        for i in range(3):
            await cog._apply_decision(make_message(i, 10), decision)

        self.assertEqual(self.timeouts, [])
        self.assertIn("skipped", cog._incidents[(1, 3)].timeout_status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.deleted, [0, 1, 2])

    async def test_repeated_deletes_escalate_to_timeout(self) -> None:
        cog, make_message = self._setup(member_outranks_bot=False)
        decision = ModerationDecision(action=ModerationAction.DELETE, reason="blocked_domain")

        for i in range(3):
            await cog._apply_decision(make_message(i, 10), decision)

        self.assertEqual(len(self.timeouts), 1)
        self.assertEqual(self.cases, ["delete", "timeout"])

    async def test_everyone_ping_warns_on_the_third_try_only(self) -> None:
        from collections import defaultdict

        from bulmaai.services.moderation import ModerationState

        cog, make_message = self._setup(member_outranks_bot=True)
        cog._state = ModerationState()
        cog._everyone_ping_events = defaultdict(list)
        cog._send_delete_notice = AsyncMock()
        cog._issue_warn_strike = AsyncMock()
        decision = ModerationDecision(action=ModerationAction.DELETE, reason="everyone_ping")

        for i in range(2):
            await cog._apply_decision(make_message(i, 10), decision)
        cog._issue_warn_strike.assert_not_awaited()
        await cog._apply_decision(make_message(2, 10), decision)
        cog._issue_warn_strike.assert_awaited_once()
        await cog._apply_decision(make_message(3, 10), decision)
        cog._issue_warn_strike.assert_awaited_once()


class ImageBurstConfirmationTests(unittest.TestCase):
    def _post(self, at: float, channel: int, *sigs):
        from bulmaai.services.moderation import ImagePost

        return ImagePost(posted_at=at, channel_id=channel, signatures=tuple(sigs))

    def test_few_distinct_screenshots_in_one_channel_are_not_confirmed(self) -> None:
        from bulmaai.services.moderation import confirm_image_burst

        posts = [self._post(0, 1, (100, 800, 600), (200, 800, 600)), self._post(3, 1, (300, 1920, 1080))]
        self.assertIsNone(confirm_image_burst(posts, now=10, window_seconds=28, min_images=3))

    def test_cross_channel_repost_or_volume_is_confirmed(self) -> None:
        from bulmaai.services.moderation import confirm_image_burst

        cross = [self._post(0, 1, (1, 2, 3)), self._post(1, 2, (4, 5, 6))]
        repost = [self._post(0, 1, (1, 2, 3)), self._post(1, 1, (1, 2, 3), (7, 8, 9))]
        volume = [self._post(i, 1, (i, 1, 1), (i, 2, 2)) for i in range(3)]
        for posts in (cross, repost, volume):
            self.assertIsNotNone(confirm_image_burst(posts, now=5, window_seconds=28, min_images=3))


class ImageBurstDelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfirmed_burst_takes_no_action_and_confirmed_burst_acts_once(self) -> None:
        import asyncio

        from bulmaai.cogs import moderation as moderation_cog
        from bulmaai.services.moderation import ImagePost

        self.addCleanup(setattr, moderation_cog, "IMAGE_BURST_CONFIRM_SECONDS", moderation_cog.IMAGE_BURST_CONFIRM_SECONDS)
        moderation_cog.IMAGE_BURST_CONFIRM_SECONDS = 0.01
        cog = ModerationCog.__new__(ModerationCog)
        cog.bot = SimpleNamespace(settings=SimpleNamespace(moderation_image_burst_window_seconds=20, moderation_image_burst_count=3))
        cog._incidents, cog._pending_image_bursts = {}, {}
        applied: list[int] = []

        async def apply(message, decision):
            applied.append(message.id)

        cog._apply_decision = apply
        decision = ModerationDecision(action=ModerationAction.TIMEOUT, reason="image burst", details="3 images")
        messages = [
            SimpleNamespace(id=i, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=3), attachments=[])
            for i in range(2)
        ]
        now = time.monotonic()

        cog._recent_images = {(1, 3): {0: ImagePost(now, 1, ((1, 1, 1),)), 1: ImagePost(now, 1, ((2, 2, 2),))}}
        await asyncio.gather(*(cog._confirm_then_apply_image_burst(m, decision) for m in messages))
        self.assertEqual(applied, [])

        cog._recent_images = {(1, 3): {0: ImagePost(now, 1, ((1, 1, 1),)), 1: ImagePost(now, 2, ((2, 2, 2),))}}
        await asyncio.gather(*(cog._confirm_then_apply_image_burst(m, decision) for m in messages))
        self.assertEqual(applied, [0, 1])
        self.assertEqual(cog._pending_image_bursts, {})


class IsAdminTests(unittest.TestCase):
    def test_is_admin_is_false_for_plain_user_from_dms(self) -> None:
        from bulmaai.utils.permissions import is_admin

        self.assertFalse(is_admin(SimpleNamespace(id=1, name="dm-user")))
        self.assertTrue(is_admin(SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True))))


if __name__ == "__main__":
    unittest.main()
