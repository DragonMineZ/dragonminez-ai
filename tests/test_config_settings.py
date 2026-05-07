import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.config import load_settings


class ConfigSettingsTests(unittest.TestCase):
    def test_ai_latency_settings_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AI_SUPPORT_DEBOUNCE_SECONDS": "0",
                "OPENAI_SUPPORT_FAST_REASONING_EFFORT": "low",
                "OPENAI_SUPPORT_FAST_CONFIDENCE_SCORE": "0.83",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.ai_support_debounce_seconds, 0)
        self.assertEqual(settings.openai_support_fast_reasoning_effort, "low")
        self.assertEqual(settings.openai_support_fast_confidence_score, 0.83)

    def test_phishing_feed_source_urls_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MODERATION_PHISHING_DOMAIN_FEED_URL": "https://example.test/domains.txt",
                "MODERATION_PHISHING_URL_FEED_URL": "https://example.test/urls.txt",
                "MODERATION_PHISHING_DOMAIN_SHA256_URL": "https://example.test/domains.sha256",
                "MODERATION_PHISHING_URL_SHA256_URL": "https://example.test/urls.sha256",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.moderation_phishing_domain_feed_url, "https://example.test/domains.txt")
        self.assertEqual(settings.moderation_phishing_url_feed_url, "https://example.test/urls.txt")
        self.assertEqual(settings.moderation_phishing_domain_sha256_url, "https://example.test/domains.sha256")
        self.assertEqual(settings.moderation_phishing_url_sha256_url, "https://example.test/urls.sha256")


if __name__ == "__main__":
    unittest.main()
