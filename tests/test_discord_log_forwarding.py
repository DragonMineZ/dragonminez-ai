import logging
import unittest

from bulmaai.services.discord_log_forwarding import (
    build_log_embed_payload,
    sanitize_log_text,
    should_forward_record,
)


class DiscordLogForwardingTests(unittest.TestCase):
    def test_warning_and_marked_info_records_are_forwarded(self) -> None:
        warning_record = logging.LogRecord("bulmaai.test", logging.WARNING, __file__, 1, "warn", (), None)
        info_record = logging.LogRecord("bulmaai.test", logging.INFO, __file__, 1, "info", (), None)
        marked_info = logging.LogRecord("bulmaai.test", logging.INFO, __file__, 1, "ready", (), None)
        marked_info.discord_forward = True

        self.assertTrue(should_forward_record(warning_record))
        self.assertFalse(should_forward_record(info_record))
        self.assertTrue(should_forward_record(marked_info))

    def test_payload_sanitizes_secrets_and_preserves_context_fields(self) -> None:
        record = logging.LogRecord(
            "bulmaai.cogs.ai_tickets",
            logging.ERROR,
            __file__,
            12,
            "OpenAI failed with OPENAI_KEY=sk-test1234567890 while handling user content",
            (),
            None,
        )
        record.guild_id = 111
        record.channel_id = 222
        record.user_id = 333
        record.message_id = 444

        payload = build_log_embed_payload(record)

        self.assertIn("ERROR | bulmaai.cogs.ai_tickets", payload.title)
        self.assertNotIn("sk-test", payload.description)
        self.assertIn("[redacted]", payload.description)
        self.assertEqual(payload.fields["guild_id"], "111")
        self.assertEqual(payload.fields["channel_id"], "222")
        self.assertEqual(payload.fields["user_id"], "333")
        self.assertEqual(payload.fields["message_id"], "444")

    def test_sanitize_log_text_redacts_common_secret_shapes(self) -> None:
        sanitized = sanitize_log_text("Authorization: Bearer abc.def.ghi and token=supersecret")

        self.assertNotIn("abc.def.ghi", sanitized)
        self.assertNotIn("supersecret", sanitized)
        self.assertGreaterEqual(sanitized.count("[redacted]"), 2)


if __name__ == "__main__":
    unittest.main()
