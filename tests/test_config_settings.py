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

    def test_escalation_and_budget_settings_default_to_free_pools(self) -> None:
        with patch.dict(os.environ, {"OPENAI_DAILY_BILLED_TOKEN_LIMIT": "5000"}, clear=False):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.openai_support_model, "gpt-5-mini")
        self.assertEqual(settings.openai_support_escalation_model, "gpt-5")
        self.assertEqual(settings.ai_support_escalation_confidence, 0.7)
        self.assertEqual(settings.openai_daily_small_token_limit, 2_250_000)
        self.assertEqual(settings.openai_daily_big_token_limit, 225_000)
        self.assertEqual(settings.openai_daily_billed_token_limit, 5000)

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
                "DEV_JAR_DOWNLOAD_WEBHOOK_PATH": "/dmz-dev-jar",
                "DEV_JAR_DOWNLOAD_DOWNLOAD_PATH": "/dev-download",
                "DEV_JAR_DOWNLOAD_UPLOAD_DIR": "/custom/env/dev-jars",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.dev_jar_download_channel_id, 1223439164121419838)
        self.assertEqual(settings.dev_jar_download_public_base_url, "https://downloads.example.test")
        self.assertEqual(settings.dev_jar_download_upload_dir, "/custom/env/dev-jars")
        self.assertEqual(settings.dev_jar_download_webhook_path, "/dmz-dev-jar")
        self.assertEqual(settings.dev_jar_download_download_path, "/dev-download")

    def test_dev_jar_review_channel_settings_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {"DEV_JAR_REVIEW_CHANNEL_ID": "111222333444555666"},
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.dev_jar_review_channel_id, 111222333444555666)

    def test_dev_jar_review_channel_defaults_to_staff_devs_channel(self) -> None:
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

        self.assertEqual(settings.dev_jar_review_channel_id, 1370061119586173070)

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

    def test_patreon_oauth_settings_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_OAUTH_CLIENT_ID": "discord-client-id",
                "DISCORD_OAUTH_CLIENT_SECRET": "discord-client-secret",
                "DISCORD_OAUTH_REDIRECT_URI": "https://downloads.example.test/beta-access/discord/callback",
                "PATREON_OAUTH_CLIENT_ID": "patreon-client-id",
                "PATREON_OAUTH_CLIENT_SECRET": "patreon-client-secret",
                "PATREON_WEBHOOK_SECRET": "patreon-webhook-secret",
                "PATREON_ELIGIBLE_TIER_IDS": "tier-contributor,tier-benefactor",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.patreon_oauth_client_id, "patreon-client-id")
        self.assertEqual(settings.patreon_oauth_client_secret, "patreon-client-secret")
        self.assertEqual(settings.patreon_webhook_secret, "patreon-webhook-secret")
        self.assertEqual(settings.patreon_eligible_tier_ids, ("tier-contributor", "tier-benefactor"))
        self.assertEqual(
            settings.patreon_oauth_redirect_uri,
            "https://downloads.dragonminez.com/patreon/oauth/callback",
        )
        self.assertEqual(settings.discord_oauth_client_id, "discord-client-id")
        self.assertEqual(settings.discord_oauth_client_secret, "discord-client-secret")
        self.assertEqual(
            settings.discord_oauth_redirect_uri,
            "https://downloads.example.test/beta-access/discord/callback",
        )

    def test_beta_access_oauth_redirect_defaults_to_downloads_domain(self) -> None:
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
            settings.discord_oauth_redirect_uri,
            "https://downloads.dragonminez.com/beta-access/discord/callback",
        )

    def test_patreon_eligible_tier_ids_default_to_actual_patreon_tiers(self) -> None:
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

        self.assertEqual(settings.patreon_eligible_tier_ids, ("23999392", "23999460"))

    def test_patch_notes_location_is_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATCH_NOTES_REPO": "dragonminez-notes",
                "PATCH_NOTES_BRANCH": "main",
                "PATCH_NOTES_FILE_PATH": "PATCH_NOTES/PATCH_NOTES-v2.1.1.md",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.patch_notes_repo, "dragonminez-notes")
        self.assertEqual(settings.patch_notes_branch, "main")
        self.assertEqual(settings.patch_notes_file_path, "PATCH_NOTES/PATCH_NOTES-v2.1.1.md")

    def test_dev_jar_announcement_and_role_ids_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEV_JAR_ANNOUNCEMENT_CHANNEL_IDS": "111111111111111111,222222222222222222",
                "DEV_JAR_PATREON_ROLE_IDS": "333333333333333333",
                "DEV_JAR_TESTER_ROLE_IDS": "444444444444444444,555555555555555555",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(
            settings.dev_jar_announcement_channel_ids,
            (111111111111111111, 222222222222222222),
        )
        self.assertEqual(settings.dev_jar_patreon_role_ids, (333333333333333333,))
        self.assertEqual(
            settings.dev_jar_tester_role_ids,
            (444444444444444444, 555555555555555555),
        )

    def test_dev_jar_announcement_and_role_ids_default_to_current_values(self) -> None:
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
            settings.dev_jar_announcement_channel_ids,
            (1516564287210913932, 1453303311330709674),
        )
        self.assertEqual(
            settings.dev_jar_patreon_role_ids,
            (1287877272224665640, 1287877305259130900),
        )
        self.assertEqual(settings.dev_jar_tester_role_ids, (1286814599215317034,))

    def test_dev_jar_download_upload_dir_defaults_when_env_missing(self) -> None:
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

        self.assertEqual(settings.dev_jar_download_upload_dir, "/var/www/dragonminez/dev-jars")

    def test_patreon_staff_and_role_ids_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATREON_ADMIN_PING_ROLE_ID": "777777777777777777",
                "PATREON_CONTRIBUTOR_ROLE_ID": "888888888888888888",
                "PATREON_BENEFACTOR_ROLE_ID": "999999999999999999",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.patreon_admin_ping_role_id, 777777777777777777)
        self.assertEqual(settings.patreon_contributor_role_id, 888888888888888888)
        self.assertEqual(settings.patreon_benefactor_role_id, 999999999999999999)

    def test_patreon_staff_and_role_ids_default_to_current_values(self) -> None:
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

        self.assertEqual(settings.patreon_admin_ping_role_id, 1309022450671161476)
        self.assertEqual(settings.patreon_contributor_role_id, 1287877272224665640)
        self.assertEqual(settings.patreon_benefactor_role_id, 1287877305259130900)

    def test_patch_notes_location_defaults_to_current_v2_1_1(self) -> None:
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

        self.assertEqual(settings.patch_notes_repo, "dragonminez")
        self.assertEqual(settings.patch_notes_branch, "v2.2")
        self.assertEqual(settings.patch_notes_file_path, "PATCH_NOTES")

    def test_ticket_close_settings_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AI_TICKET_RESOLVE_MIN_CONFIDENCE": "0.7",
                "AI_TICKET_RESOLVE_PROMPT_EXPONENT": "2.5",
                "AI_TICKET_CLOSE_DELAY_SECONDS": "0",
                "OPENAI_TICKET_SUMMARY_MODEL": "gpt-test",
                "OPENAI_TICKET_VECTOR_STORE_ID": "vs_tickets",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertEqual(settings.ai_ticket_resolve_min_confidence, 0.7)
        self.assertEqual(settings.ai_ticket_resolve_prompt_exponent, 2.5)
        self.assertEqual(settings.ai_ticket_close_delay_seconds, 0)
        self.assertEqual(settings.openai_ticket_summary_model, "gpt-test")
        self.assertEqual(settings.openai_ticket_vector_store_id, "vs_tickets")

if __name__ == "__main__":
    unittest.main()
