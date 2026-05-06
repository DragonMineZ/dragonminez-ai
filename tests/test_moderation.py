import unittest

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


if __name__ == "__main__":
    unittest.main()
