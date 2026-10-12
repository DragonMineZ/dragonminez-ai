import os
import types
import unittest
from datetime import datetime, timezone
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
        curseforge_project_id=1,
    )


_DEV_JAR = ("dragonminez-2.2.0__abcdef123456.jar", datetime(2026, 9, 28, tzinfo=timezone.utc))


def _curseforge():
    return types.SimpleNamespace(last_processed_at=datetime(2026, 9, 15, tzinfo=timezone.utc))


def _closed(closed_at):
    return {"number": 7, "title": "Crash", "state": "closed", "state_reason": "completed",
            "closed_at": closed_at, "html_url": "https://github.com/x/7"}


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
        with (
            patch.object(ai_tools.GitHubService, "search_issues", AsyncMock(return_value=items)) as search,
            patch.object(ai_tools, "get_curseforge_project_state", AsyncMock(return_value=_curseforge())),
            patch.object(ai_tools, "get_published_dev_jar_state", AsyncMock(return_value=_DEV_JAR)),
        ):
            result = await ai_tools.search_known_issues("Namek crash", _bot_context=types.SimpleNamespace(settings=_settings()))

        search.assert_awaited_once_with("Namek crash", per_page=8)
        self.assertEqual(
            result,
            {"issues": [{"number": 1, "title": "Crash on Namek", "state": "open", "labels": ["bug"],
                         "updated_at": "2026-09-20", "url": "https://github.com/x/1"}]},
        )

    async def _search(self, items, *, roles=(), curseforge=None, dev_jar=None, release_error=None):
        member = types.SimpleNamespace(roles=[types.SimpleNamespace(id=role) for role in roles])
        guild = types.SimpleNamespace(get_member=lambda user_id: member if user_id == 123 else None)
        bot = types.SimpleNamespace(settings=_settings(), guilds=[guild])
        curseforge_mock = (
            AsyncMock(side_effect=release_error) if release_error else AsyncMock(return_value=curseforge or _curseforge())
        )
        with (
            patch.object(ai_tools.GitHubService, "search_issues", AsyncMock(return_value=items)),
            patch.object(ai_tools, "get_curseforge_project_state", curseforge_mock),
            patch.object(ai_tools, "get_published_dev_jar_state", AsyncMock(return_value=dev_jar or _DEV_JAR)),
        ):
            return await ai_tools.search_known_issues("x", discord_user_id="123", _bot_context=bot)

    async def test_patron_sees_dev_jar_fix(self) -> None:
        for roles in ((10,), (20,)):
            result = await self._search([_closed("2026-09-25T00:00:00Z")], roles=roles)
            self.assertEqual(result["issues"][0]["fix"], "in_dev_jar")
            self.assertEqual(result["issues"][0]["dev_jar_version"], "2.2.0")

    async def test_non_patron_gets_pending_release_without_dev_jar_details(self) -> None:
        result = await self._search([_closed("2026-09-25T00:00:00Z")], roles=(99,))
        self.assertEqual(result["issues"][0]["fix"], "pending_release")
        rendered = repr(result)
        for leak in ("2.2.0", "dev_jar", "abcdef1", ".jar"):
            self.assertNotIn(leak, rendered)

    async def test_unknown_member_is_not_patron(self) -> None:
        result = await self._search([_closed("2026-09-25T00:00:00Z")], roles=(10,))
        bot = types.SimpleNamespace(settings=_settings(), guilds=[])
        with (
            patch.object(ai_tools.GitHubService, "search_issues", AsyncMock(return_value=[_closed("2026-09-25T00:00:00Z")])),
            patch.object(ai_tools, "get_curseforge_project_state", AsyncMock(return_value=_curseforge())),
            patch.object(ai_tools, "get_published_dev_jar_state", AsyncMock(return_value=_DEV_JAR)),
        ):
            unknown = await ai_tools.search_known_issues("x", discord_user_id="123", _bot_context=bot)
        self.assertEqual(result["issues"][0]["fix"], "in_dev_jar")
        self.assertEqual(unknown["issues"][0]["fix"], "pending_release")

    async def test_fix_after_dev_jar_is_pending_even_for_patrons(self) -> None:
        result = await self._search([_closed("2026-09-29T00:00:00Z")], roles=(10,))
        self.assertEqual(result["issues"][0]["fix"], "pending_release")

    async def test_fix_in_public_release(self) -> None:
        for roles in ((10,), ()):
            result = await self._search([_closed("2026-09-10T00:00:00Z")], roles=roles)
            self.assertEqual(result["issues"][0]["fix"], "in_public_release")
            self.assertNotIn("dev_jar_version", result["issues"][0])

    async def test_not_planned_and_open_issues(self) -> None:
        not_planned = {**_closed("2026-09-10T00:00:00Z"), "state_reason": "not_planned"}
        open_issue = {"number": 3, "title": "Open", "state": "open", "state_reason": None}
        result = await self._search([not_planned, open_issue], roles=(10,))
        self.assertEqual(result["issues"][0]["fix"], "not_fixed")
        self.assertNotIn("fix", result["issues"][1])

    async def test_release_lookup_failure_omits_fix(self) -> None:
        result = await self._search([_closed("2026-09-10T00:00:00Z")], roles=(10,), release_error=RuntimeError("db"))
        self.assertEqual(len(result["issues"]), 1)
        self.assertNotIn("fix", result["issues"][0])

    def test_schema_takes_requester_id_and_is_identity_tool(self) -> None:
        schema = ai_tools.TOOL_SCHEMAS["search_known_issues"]
        self.assertIn("discord_user_id", schema["parameters"]["properties"])
        self.assertIn("search_known_issues", ai_tools.IDENTITY_TOOLS)


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

    async def test_ticket_account_is_fetched_once_and_reused(self) -> None:
        ai_tools._account_cache.clear()
        with (
            patch.object(ai_tools, "get_latest_releases", AsyncMock(return_value={})),
            patch.object(ai_tools, "get_patreon_status", AsyncMock(return_value={"linked": True})) as status,
        ):
            first = await ai_tools.build_prefetch_lines(bot=object(), user_id=1, text="hi", account_key=(9, 1))
            second = await ai_tools.build_prefetch_lines(bot=object(), user_id=1, text="thanks", account_key=(9, 1))
        status.assert_awaited_once()
        self.assertIn('requester_account: {"linked": true}', first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
