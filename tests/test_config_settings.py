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
                "OPENAI_SUPPORT_VECTOR_STORE_IDS": "vs_docs, vs_tickets",
                "OPENAI_SUPPORT_FILE_SEARCH_MAX_RESULTS": "8",
                "OPENAI_SUPPORT_STORE_RESPONSES": "true",
                "OPENAI_FAQ_SUGGESTION_MODEL": "gpt-5.4-mini",
                "OPENAI_FAQ_VECTOR_STORE_ID": "vs_faq",
                "OPENAI_FAQ_GENERATED_PATH": "data/knowledge/generated/faq.md",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.ai_support_debounce_seconds, 0)
        self.assertEqual(settings.openai_support_fast_reasoning_effort, "low")
        self.assertEqual(settings.openai_support_vector_store_ids, ("vs_docs", "vs_tickets"))
        self.assertEqual(settings.openai_support_file_search_max_results, 8)
        self.assertTrue(settings.openai_support_store_responses)
        self.assertEqual(settings.openai_faq_suggestion_model, "gpt-5.4-mini")
        self.assertEqual(settings.openai_faq_vector_store_id, "vs_faq")
        self.assertEqual(settings.openai_faq_generated_path, "data/knowledge/generated/faq.md")

    def test_phishdestroy_settings_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PHISHDESTROY_ENABLED": "false",
                "PHISHDESTROY_API_BASE_URL": "https://api.example.test",
                "PHISHDESTROY_ACTION": "delete",
                "PHISHDESTROY_TIMEOUT_SECONDS": "4",
                "PHISHDESTROY_RECOVERY_INTERVAL_SECONDS": "120",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertFalse(settings.phishdestroy_enabled)
        self.assertEqual(settings.phishdestroy_api_base_url, "https://api.example.test")
        self.assertEqual(settings.phishdestroy_action, "delete")
        self.assertEqual(settings.phishdestroy_timeout_seconds, 4)
        self.assertEqual(settings.phishdestroy_recovery_interval_seconds, 120)

    def test_phishdestroy_defaults_are_lightweight_api_checks(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "dummy-discord-token",
                "OPENAI_KEY": "dummy-openai-key",
                "GH_APP_PRIVATE_KEY_PEM": "dummy-github-key",
            },
            clear=True,
        ):
            settings = load_settings(include_overrides=False)

        self.assertTrue(settings.phishdestroy_enabled)
        self.assertEqual(settings.phishdestroy_api_base_url, "https://api.destroy.tools")
        self.assertEqual(settings.phishdestroy_action, "alert")
        self.assertEqual(settings.phishdestroy_timeout_seconds, 5)
        self.assertEqual(settings.phishdestroy_recovery_interval_seconds, 300)

    def test_dev_jar_download_settings_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEV_JAR_DOWNLOAD_CHANNEL_ID": "1223439164121419838",
                "DEV_JAR_DOWNLOAD_PUBLIC_BASE_URL": "https://downloads.example.test",
                "DEV_JAR_DOWNLOAD_UPLOAD_DIR": "/srv/dmz/dev-jars",
                "DEV_JAR_DOWNLOAD_WEBHOOK_PATH": "/dmz-dev-jar",
                "DEV_JAR_DOWNLOAD_DOWNLOAD_PATH": "/dev-download",
                "DEV_JAR_DOWNLOAD_OAUTH_CALLBACK_PATH": "/dev-download/oauth/callback",
                "DEV_JAR_DOWNLOAD_BYPASS_ROLE_IDS": "1341596685339725885",
                "PATREON_OAUTH_CLIENT_ID": "client-id",
                "PATREON_OAUTH_CLIENT_SECRET": "client-secret",
                "DEV_JAR_DOWNLOAD_WEBHOOK_SECRET": "webhook-secret",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.dev_jar_download_channel_id, 1223439164121419838)
        self.assertEqual(settings.dev_jar_download_public_base_url, "https://downloads.example.test")
        self.assertEqual(settings.dev_jar_download_upload_dir, "/srv/dmz/dev-jars")
        self.assertEqual(settings.dev_jar_download_webhook_path, "/dmz-dev-jar")
        self.assertEqual(settings.dev_jar_download_download_path, "/dev-download")
        self.assertEqual(settings.dev_jar_download_oauth_callback_path, "/dev-download/oauth/callback")
        self.assertEqual(settings.dev_jar_download_bypass_role_ids, (1341596685339725885,))
        self.assertEqual(settings.patreon_oauth_client_id, "client-id")
        self.assertEqual(settings.patreon_oauth_client_secret, "client-secret")
        self.assertEqual(settings.dev_jar_download_webhook_secret, "webhook-secret")

    def test_dev_jar_public_base_url_defaults_to_downloads_domain(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "dummy-discord-token",
                "OPENAI_KEY": "dummy-openai-key",
                "GH_APP_PRIVATE_KEY_PEM": "dummy-github-key",
            },
            clear=True,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(
            settings.dev_jar_download_public_base_url,
            "https://downloads.dragonminez.com",
        )


if __name__ == "__main__":
    unittest.main()
