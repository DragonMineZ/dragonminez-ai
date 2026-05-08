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
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.ai_support_debounce_seconds, 0)
        self.assertEqual(settings.openai_support_fast_reasoning_effort, "low")
        self.assertEqual(settings.openai_support_vector_store_ids, ("vs_docs", "vs_tickets"))
        self.assertEqual(settings.openai_support_file_search_max_results, 8)
        self.assertTrue(settings.openai_support_store_responses)

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


if __name__ == "__main__":
    unittest.main()
