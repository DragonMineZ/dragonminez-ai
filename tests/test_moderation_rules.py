import unittest

from bulmaai.services.moderation import (
    AttachmentInfo,
    MessageSignal,
    ModerationAction,
    ModerationConfig,
    ModerationState,
    defang_domain,
    evaluate_message,
    extract_urls,
)


class ModerationRulesTests(unittest.TestCase):
    def test_extract_urls_handles_obfuscation_and_discord_invites(self) -> None:
        urls = extract_urls("visit hxxps://bad[.]site/path and discord.gg/abc123")
        domains = {url.domain for url in urls}

        self.assertIn("bad.site", domains)
        self.assertIn("discord.gg", domains)

    def test_blocked_domain_is_delete_action(self) -> None:
        config = ModerationConfig(blocked_domains=("bad.site",))
        signal = MessageSignal(
            guild_id=1,
            channel_id=2,
            author_id=3,
            content="check https://bad.site/free",
        )

        decision = evaluate_message(signal, config, ModerationState(), now=100.0)

        self.assertEqual(decision.action, ModerationAction.DELETE)
        self.assertIn("bad[.]site", decision.defanged_domains)

    def test_image_burst_deletes_on_threshold(self) -> None:
        config = ModerationConfig(image_burst_count=3, image_burst_window_seconds=20)
        state = ModerationState()
        attachment = AttachmentInfo(filename="spam.png", content_type="image/png", size=10)

        first = evaluate_message(
            MessageSignal(guild_id=1, channel_id=2, author_id=3, content="", attachments=(attachment,)),
            config,
            state,
            now=100.0,
        )
        second = evaluate_message(
            MessageSignal(guild_id=1, channel_id=2, author_id=3, content="", attachments=(attachment,)),
            config,
            state,
            now=105.0,
        )
        third = evaluate_message(
            MessageSignal(guild_id=1, channel_id=2, author_id=3, content="", attachments=(attachment,)),
            config,
            state,
            now=110.0,
        )

        self.assertEqual(first.action, ModerationAction.ALLOW)
        self.assertEqual(second.action, ModerationAction.ALLOW)
        self.assertEqual(third.action, ModerationAction.DELETE)
        self.assertIn("image burst", third.reason)

    def test_defang_domain(self) -> None:
        self.assertEqual(defang_domain("sub.example.com"), "sub[.]example[.]com")


if __name__ == "__main__":
    unittest.main()
