import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services.patreon_access import PatreonMemberDetails
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, SECRET, make_bot

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
TARGET_ID = 777


def link_row(**overrides):
    row = {
        "patreon_full_name": "Some Patron",
        "patron_status": "active_patron",
        "tier_ids": ["23999392"],
        "last_charge_date": NOW,
        "entitlement_active": True,
        "patreon_member_id": "member-1",
        "linked_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def grant_row(owner, beneficiary, nick, kind="self", **overrides):
    row = {
        "id": 1,
        "owner_discord_user_id": owner,
        "beneficiary_discord_user_id": beneficiary,
        "beneficiary_discord_username": f"user{beneficiary}",
        "minecraft_username": nick,
        "kind": kind,
        "active": True,
        "source_pr_url": "https://github.com/pr/1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


class PatreonPeoplePanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    def _pool(self, *, link=None, grants=()):
        return SimpleNamespace(fetchrow=AsyncMock(return_value=link), fetch=AsyncMock(return_value=list(grants)))

    async def test_helper_cannot_view_person(self):
        self.login(HELPER_ID)
        response = await self.client.get(f"/api/patreon/people/{TARGET_ID}")
        self.assertEqual(response.status, 403)

    async def test_invalid_user_id_is_rejected(self):
        self.login(OWNER_ID)
        response = await self.client.get("/api/patreon/people/not-a-number")
        self.assertEqual(response.status, 400)

    async def test_link_and_grants_without_creator_token(self):
        object.__setattr__(self.bot.settings, "PATREON_CREATOR_TOKEN", None)  # the VPS env has a real one
        self.login(OWNER_ID)
        pool = self._pool(link=link_row(), grants=[grant_row(TARGET_ID, TARGET_ID, "Steve")])
        with patch("bulmaai.web.routes_patreon.get_pool", AsyncMock(return_value=pool)):
            data = await (await self.client.get(f"/api/patreon/people/{TARGET_ID}")).json()
        self.assertEqual(data["link"]["patreon_full_name"], "Some Patron")
        self.assertEqual(data["link"]["tier_ids"], ["23999392"])
        self.assertEqual(len(data["grants"]), 1)
        self.assertEqual(data["grants"][0]["minecraft_username"], "Steve")
        self.assertIsNone(data["live"])
        self.assertIn("creator token", data["live_error"])

    async def test_no_link_but_has_grants_is_still_useful(self):
        self.login(OWNER_ID)
        pool = self._pool(link=None, grants=[grant_row(10, TARGET_ID, "GiftedName", kind="gift")])
        with patch("bulmaai.web.routes_patreon.get_pool", AsyncMock(return_value=pool)):
            data = await (await self.client.get(f"/api/patreon/people/{TARGET_ID}")).json()
        self.assertIsNone(data["link"])
        self.assertIsNone(data["live"])
        self.assertIsNone(data["live_error"])
        self.assertEqual(len(data["grants"]), 1)
        self.assertEqual(data["grants"][0]["kind"], "gift")

    async def test_neither_link_nor_grants_is_a_friendly_empty_state(self):
        self.login(OWNER_ID)
        pool = self._pool(link=None, grants=[])
        with patch("bulmaai.web.routes_patreon.get_pool", AsyncMock(return_value=pool)):
            response = await self.client.get(f"/api/patreon/people/{TARGET_ID}")
            data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertIsNone(data["link"])
        self.assertEqual(data["grants"], [])
        self.assertIn("user", data)

    async def test_live_data_is_fetched_with_creator_token(self):
        self.login(OWNER_ID)
        object.__setattr__(self.bot.settings, "PATREON_CREATOR_TOKEN", "creator-tok")
        pool = self._pool(link=link_row(), grants=[])
        details = PatreonMemberDetails(
            patron_status="active_patron",
            tier_ids=("23999392",),
            tier_names={"23999392": "Contributor"},
            currently_entitled_amount_cents=500,
            lifetime_support_cents=6000,
            pledge_relationship_start=NOW,
            last_charge_date=NOW,
            last_charge_status="Paid",
            next_charge_date=NOW,
            pledge_cadence="month",
        )
        fake_client = SimpleNamespace(fetch_member_details=AsyncMock(return_value=details))
        with (
            patch("bulmaai.web.routes_patreon.get_pool", AsyncMock(return_value=pool)),
            patch("bulmaai.web.routes_patreon.PatreonCreatorClient", return_value=fake_client) as ctor,
        ):
            data = await (await self.client.get(f"/api/patreon/people/{TARGET_ID}")).json()
        ctor.assert_called_once_with(creator_token="creator-tok", campaign_id=self.bot.settings.PATREON_CAMPAIGN_ID)
        fake_client.fetch_member_details.assert_awaited_once_with("member-1")
        self.assertIsNone(data["live_error"])
        self.assertEqual(data["live"]["currently_entitled_amount_cents"], 500)
        self.assertEqual(data["live"]["tier_names"], {"23999392": "Contributor"})
        self.assertEqual(data["live"]["pledge_cadence"], "month")

    async def test_live_data_failure_degrades_gracefully(self):
        self.login(OWNER_ID)
        object.__setattr__(self.bot.settings, "PATREON_CREATOR_TOKEN", "creator-tok")
        pool = self._pool(link=link_row(), grants=[])
        fake_client = SimpleNamespace(fetch_member_details=AsyncMock(side_effect=RuntimeError("Patreon is down")))
        with (
            patch("bulmaai.web.routes_patreon.get_pool", AsyncMock(return_value=pool)),
            patch("bulmaai.web.routes_patreon.PatreonCreatorClient", return_value=fake_client),
        ):
            response = await self.client.get(f"/api/patreon/people/{TARGET_ID}")
            data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertIsNone(data["live"])
        self.assertIn("Patreon is down", data["live_error"])

    async def test_link_without_member_id_skips_live_lookup(self):
        self.login(OWNER_ID)
        object.__setattr__(self.bot.settings, "PATREON_CREATOR_TOKEN", "creator-tok")
        pool = self._pool(link=link_row(patreon_member_id=None), grants=[])
        with (
            patch("bulmaai.web.routes_patreon.get_pool", AsyncMock(return_value=pool)),
            patch("bulmaai.web.routes_patreon.PatreonCreatorClient") as ctor,
        ):
            data = await (await self.client.get(f"/api/patreon/people/{TARGET_ID}")).json()
        ctor.assert_not_called()
        self.assertIsNone(data["live"])
        self.assertIn("member ID", data["live_error"])


if __name__ == "__main__":
    unittest.main()
