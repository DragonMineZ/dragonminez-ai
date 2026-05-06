import asyncio
import logging
import unittest

from bulmaai.services.discord_log_forwarding import (
    DiscordLogForwardingQueue,
    build_log_embed_payload,
)


def make_record(level: int = logging.ERROR, **extras: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "bulmaai.test",
        level,
        __file__,
        10,
        "failed token=%s",
        ("raw-secret",),
        None,
    )
    for key, value in extras.items():
        setattr(record, key, value)
    return record


class DiscordLogExtraMetadataTests(unittest.TestCase):
    def test_payload_includes_safe_extras_and_skips_sensitive_content_extras(self) -> None:
        record = make_record(
            request_id="req-123",
            shard="west",
            api_key="secret-value",
            user_content="raw player message",
        )

        payload = build_log_embed_payload(record)

        self.assertEqual(payload.fields["request_id"], "req-123")
        self.assertEqual(payload.fields["shard"], "west")
        self.assertNotIn("api_key", payload.fields)
        self.assertNotIn("user_content", payload.fields)
        self.assertNotIn("raw-secret", payload.description)
        self.assertIn("[redacted]", payload.description)


class DiscordLogForwardingQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_sends_in_order_and_continues_after_sender_error(self) -> None:
        sent_markers: list[str] = []

        async def sender(payload) -> None:
            marker = payload.fields["marker"]
            await asyncio.sleep(0)
            if marker == "bad":
                raise RuntimeError("discord send failed")
            sent_markers.append(marker)

        queue = DiscordLogForwardingQueue(sender, max_queue_size=10)
        await queue.start()
        self.addAsyncCleanup(queue.stop)

        self.assertTrue(queue.enqueue(make_record(marker="first")))
        self.assertTrue(queue.enqueue(make_record(marker="bad")))
        self.assertTrue(queue.enqueue(make_record(marker="second")))

        await queue.flush()

        self.assertEqual(sent_markers, ["first", "second"])
        self.assertEqual(queue.send_error_count, 1)

    async def test_queue_drops_cleanly_when_full(self) -> None:
        async def sender(payload) -> None:
            raise AssertionError("sender should not run before start")

        queue = DiscordLogForwardingQueue(sender, max_queue_size=1)

        self.assertTrue(queue.enqueue(make_record(marker="first")))
        self.assertFalse(queue.enqueue(make_record(marker="second")))
        self.assertEqual(queue.dropped_count, 1)
        self.assertEqual(queue.queue_size, 1)


if __name__ == "__main__":
    unittest.main()
