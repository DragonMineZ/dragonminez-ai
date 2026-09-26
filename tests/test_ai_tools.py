import os
import types
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import ai_tools
from bulmaai.services.patreon_grants import PatreonGrant, PatreonGrantKind, PatreonLink


def _settings():
    return types.SimpleNamespace(
        dev_jar_patreon_role_ids=(10,),
        dev_jar_tester_role_ids=(20,),
        patreon_access_role_ids=(10,),
        GH_APP_ID="1",
        GH_INSTALLATION_ID="2",
        GH_APP_PRIVATE_KEY_PEM="pem",
        GITHUB_OWNER="DragonMineZ",
        GITHUB_BASE_BRANCH="main",
        bug_report_repo="dragonminez",
    )


class PatreonStatusToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_minimized_status_without_personal_patreon_fields(self) -> None:
        member = types.SimpleNamespace(
            roles=[types.SimpleNamespace(id=10)],
            guild_permissions=types.SimpleNamespace(administrator=False),
        )
        guild = types.SimpleNamespace(get_member=lambda user_id: member if user_id == 123 else None)
        bot = types.SimpleNamespace(settings=_settings(), guilds=[guild])
        link = PatreonLink(
            discord_user_id=123,
            discord_username="goku",
            patreon_user_id="p-secret",
            patreon_member_id="m-secret",
            patreon_full_name="Son Goku",
            patron_status="active_patron",
            tier_ids=("23999392",),
            last_charge_date="2026-09-01",
            entitlement_active=True,
        )
        grant = PatreonGrant(
            owner_discord_user_id=123,
            beneficiary_discord_user_id=456,
            beneficiary_discord_username="vegeta",
            minecraft_username="Vegeta",
            kind=PatreonGrantKind.GIFT,
            active=True,
        )
        with (
            patch.object(ai_tools, "get_patreon_link", AsyncMock(return_value=link)),
            patch.object(ai_tools, "list_active_grants_for_owner", AsyncMock(return_value=[grant])),
            patch.object(ai_tools, "list_active_gifts_for_beneficiary", AsyncMock(return_value=[])),
            patch.object(ai_tools, "get_published_dev_jar_file_name", AsyncMock(return_value="dmz.jar")),
            patch.object(ai_tools, "has_completed_dev_jar_download", AsyncMock(return_value=False)),
        ):
            status = await ai_tools.get_patreon_status("123", _bot_context=bot)

        self.assertTrue(status["has_patreon_discord_role"])
        self.assertTrue(status["can_download_dev_jar"])
        self.assertTrue(status["entitlement_active"])
        self.assertEqual(status["whitelist_entries"], [{"minecraft_username": "Vegeta", "kind": "gift"}])
        self.assertEqual(status["gifts_given"], 1)
        self.assertFalse(status["downloaded_current_dev_jar"])
        rendered = repr(status)
        for secret in ("Son Goku", "p-secret", "m-secret", "23999392", "2026-09-01"):
            self.assertNotIn(secret, rendered)


class KnownIssuesToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_drops_pull_requests_and_keeps_compact_fields(self) -> None:
        items = [
            {"number": 1, "title": "Crash on Namek", "state": "open", "labels": [{"name": "bug"}],
             "updated_at": "2026-09-20T10:00:00Z", "html_url": "https://github.com/x/1", "body": "long"},
            {"number": 2, "title": "Fix crash", "state": "closed", "pull_request": {}, "html_url": "https://github.com/x/2"},
        ]
        with patch.object(ai_tools.GitHubService, "search_issues", AsyncMock(return_value=items)) as search:
            result = await ai_tools.search_known_issues("Namek crash", _bot_context=types.SimpleNamespace(settings=_settings()))

        search.assert_awaited_once_with("Namek crash", per_page=8)
        self.assertEqual(
            result,
            {"issues": [{"number": 1, "title": "Crash on Namek", "state": "open", "labels": ["bug"],
                         "updated_at": "2026-09-20", "url": "https://github.com/x/1"}]},
        )


class PrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_data_is_only_fetched_when_the_conversation_is_about_it(self) -> None:
        with (
            patch.object(ai_tools, "get_latest_releases", AsyncMock(return_value={"v": 1})) as releases,
            patch.object(ai_tools, "get_patreon_status", AsyncMock(return_value={"linked": True})) as status,
            patch.object(ai_tools, "get_user_bug_reports", AsyncMock(side_effect=RuntimeError("db"))),
        ):
            plain = await ai_tools.build_prefetch_lines(bot=object(), user_id=1, text="how do I transform")
            account = await ai_tools.build_prefetch_lines(
                bot=object(), user_id=1, text="I pay on Patreon but can't get the dev jar, bug?"
            )

        self.assertEqual(plain, ['releases: {"v": 1}'])
        status.assert_awaited_once()
        self.assertEqual(status.await_args.args, ("1",))
        self.assertEqual(account, ['releases: {"v": 1}', 'requester_account: {"linked": true}'])
        self.assertFalse(releases.await_args_list[0].kwargs["include_patch_notes"])


if __name__ == "__main__":
    unittest.main()
