import unittest
import logging
from unittest.mock import patch

from bulmaai.services.moderation import (
    MessageSignal,
    ModerationAction,
    ModerationConfig,
    ModerationState,
    evaluate_message,
)
from bulmaai.services.phishing_feed import (
    PhishingFeedService,
    PhishingFeedSnapshot,
    canonicalize_url,
    parse_domain_feed,
    parse_url_feed,
)


class PhishingFeedParsingTests(unittest.TestCase):
    def test_domain_feed_normalizes_case_duplicates_trailing_dots_and_comments(self) -> None:
        domains = parse_domain_feed(
            """
            # comment
            Example.COM.

            example.com
            Sub.Example.COM.
            """
        )

        self.assertEqual(domains, frozenset({"example.com", "sub.example.com"}))

    def test_url_canonicalization_strips_fragments_lowercases_host_and_preserves_query(self) -> None:
        self.assertEqual(
            canonicalize_url("HTTPS://Example.COM/path?A=1#fragment"),
            "https://example.com/path?A=1",
        )
        self.assertEqual(
            canonicalize_url("https://Example.COM:443/path"),
            "https://example.com/path",
        )

    def test_url_feed_normalizes_and_deduplicates_exact_urls(self) -> None:
        urls = parse_url_feed(
            """
            # comment
            HTTPS://Example.COM/path?A=1#ignore
            https://example.com/path?A=1
            """
        )

        self.assertEqual(urls, frozenset({"https://example.com/path?A=1"}))


class FakeResponse:
    def __init__(self, *, content: bytes, text: str | None = None):
        self.content = content
        self.text = text if text is not None else content.decode("utf-8")

    def raise_for_status(self) -> None:
        return None


class PhishingFeedRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_checksum_verifies_raw_response_bytes_not_decoded_text(self) -> None:
        domains = b"Example.COM\n"
        urls = "https://bad.example/\n".encode("utf-16")

        async def fake_request(method: str, url: str, **kwargs):
            if url == "https://feeds.example/domains.txt":
                return FakeResponse(content=domains)
            if url == "https://feeds.example/urls.txt":
                return FakeResponse(
                    content=urls,
                    text="https://bad.example/\n",
                )
            if url == "https://feeds.example/domains.sha256":
                import hashlib

                return FakeResponse(content=b"", text=hashlib.sha256(domains).hexdigest())
            if url == "https://feeds.example/urls.sha256":
                import hashlib

                return FakeResponse(content=b"", text=hashlib.sha256(urls).hexdigest())
            raise AssertionError(f"unexpected URL {url}")

        service = PhishingFeedService(
            cache_dir="data/test-cache",
            max_stale_hours=24,
            domain_feed_url="https://feeds.example/domains.txt",
            url_feed_url="https://feeds.example/urls.txt",
            domain_checksum_url="https://feeds.example/domains.sha256",
            url_checksum_url="https://feeds.example/urls.sha256",
        )

        with patch("bulmaai.services.phishing_feed.http.request", side_effect=fake_request):
            domains_text, urls_text, checksums = await service._download_feeds()

        self.assertEqual(domains_text, "Example.COM\n")
        self.assertEqual(urls_text, "https://bad.example/\n")
        self.assertIn("domains_sha256", checksums)
        self.assertIn("exact_urls_sha256", checksums)

    async def test_refresh_failure_log_is_marked_for_discord_forwarding(self) -> None:
        async def fake_request(method: str, url: str, **kwargs):
            raise RuntimeError("network down")

        service = PhishingFeedService(cache_dir="data/test-cache", max_stale_hours=24)
        records: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("bulmaai.services.phishing_feed")
        handler = CaptureHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        self.addCleanup(logger.removeHandler, handler)

        with patch("bulmaai.services.phishing_feed.http.request", side_effect=fake_request):
            await service.refresh()

        self.assertEqual(len(records), 1)
        self.assertIn("Phishing feed refresh failed", records[0].getMessage())
        self.assertTrue(records[0].discord_forward)
        self.assertEqual(records[0].event, "phishing_feed_refresh_failed")


class PhishingFeedModerationTests(unittest.TestCase):
    def _signal(self, content: str) -> MessageSignal:
        return MessageSignal(guild_id=1, channel_id=2, author_id=3, content=content)

    def test_manual_blocked_domain_wins_even_if_allowed(self) -> None:
        snapshot = PhishingFeedSnapshot(domains=frozenset({"bad.example"}))
        config = ModerationConfig(
            blocked_domains=("bad.example",),
            allowed_domains=("bad.example",),
            phishing_feed_action=ModerationAction.ALERT,
        )

        decision = evaluate_message(
            self._signal("https://bad.example/free"),
            config,
            ModerationState(),
            now=100.0,
            phishing_feed_snapshot=snapshot,
        )

        self.assertEqual(decision.action, ModerationAction.DELETE)
        self.assertEqual(decision.reason, "blocked_domain")

    def test_trusted_allowed_domain_suppresses_feed_domain_hit(self) -> None:
        snapshot = PhishingFeedSnapshot(domains=frozenset({"trusted.example"}))
        config = ModerationConfig(
            allowed_domains=("trusted.example",),
            phishing_feed_action=ModerationAction.DELETE,
        )

        decision = evaluate_message(
            self._signal("https://trusted.example/login"),
            config,
            ModerationState(),
            now=100.0,
            phishing_feed_snapshot=snapshot,
        )

        self.assertEqual(decision.action, ModerationAction.ALLOW)

    def test_exact_feed_url_alerts_or_deletes_from_setting(self) -> None:
        snapshot = PhishingFeedSnapshot(
            exact_urls=frozenset({"https://bad.example/path?token=1"})
        )

        alert_decision = evaluate_message(
            self._signal("see https://BAD.example/path?token=1#secret"),
            ModerationConfig(phishing_feed_action=ModerationAction.ALERT),
            ModerationState(),
            now=100.0,
            phishing_feed_snapshot=snapshot,
        )
        delete_decision = evaluate_message(
            self._signal("see https://BAD.example/path?token=1#secret"),
            ModerationConfig(phishing_feed_action=ModerationAction.DELETE),
            ModerationState(),
            now=100.0,
            phishing_feed_snapshot=snapshot,
        )

        self.assertEqual(alert_decision.action, ModerationAction.ALERT)
        self.assertEqual(delete_decision.action, ModerationAction.DELETE)
        self.assertEqual(alert_decision.reason, "phishing_feed_url")
        self.assertIn("bad[.]example", alert_decision.defanged_domains)
        self.assertNotIn("https://bad.example/path?token=1", alert_decision.details)

    def test_empty_or_failed_feed_snapshot_does_not_raise_or_delete(self) -> None:
        snapshot = PhishingFeedSnapshot.empty(error="cache missing")

        decision = evaluate_message(
            self._signal("https://bad.example/path"),
            ModerationConfig(phishing_feed_action=ModerationAction.DELETE),
            ModerationState(),
            now=100.0,
            phishing_feed_snapshot=snapshot,
        )

        self.assertEqual(decision.action, ModerationAction.ALLOW)


if __name__ == "__main__":
    unittest.main()
