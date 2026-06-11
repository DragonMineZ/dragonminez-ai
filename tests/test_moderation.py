import time
import unittest
from datetime import timedelta
from types import SimpleNamespace

from bulmaai.cogs.moderation import ModerationCog
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

        decision = ModerationDecision(
            action=ModerationAction.TIMEOUT,
            reason="image burst",
            details="4 images across 2 messages in 20s",
            image_count=2,
        )

        await cog._apply_decision(message, decision)

        self.assertEqual(deleted, ["BulmaAI moderation: image burst"])
        self.assertEqual(len(timeout_calls), 1)
        self.assertEqual(timeout_calls[0][0], timedelta(days=7))
        purged_channel_ids = {call["channel_id"] for call in purge_calls}
        self.assertEqual(purged_channel_ids, {2, 7})
        self.assertNotIn((1, 3), cog._recent_message_channels)


if __name__ == "__main__":
    unittest.main()
