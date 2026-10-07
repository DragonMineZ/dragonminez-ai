import asyncio
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.cogs.ai_tickets import AITicketsCog
from bulmaai.services.patreon_grants import PatreonGrant, PatreonGrantKind
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, SECRET, make_bot

CATEGORY_ID = 900
TICKET_ID = 500
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def grant(owner, beneficiary, nick, kind):
    return PatreonGrant(
        owner_discord_user_id=owner,
        beneficiary_discord_user_id=beneficiary,
        beneficiary_discord_username=f"user{beneficiary}",
        minecraft_username=nick,
        kind=kind,
        active=True,
    )


class FakeTicketsCog:
    def __init__(self):
        self.disabled = set()
        self.set_ticket_ai_enabled = AsyncMock()

    def is_ticket_ai_enabled(self, channel_id):
        return channel_id not in self.disabled


class FakeFlowCog:
    def __init__(self, commit_url):
        self._lock = asyncio.Lock()
        self._auto_approve_beta_access = AsyncMock(return_value=commit_url)
        self._record_self_grant = AsyncMock()
        self._log_staff_info = AsyncMock()
        self._remove_whitelist_grants = AsyncMock()

    def _beta_access_lock(self, member_id):
        return self._lock


class TicketsPatreonPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        object.__setattr__(self.bot.settings, "ai_ticket_category_id", CATEGORY_ID)
        guild = self.bot.guilds[0]
        ticket = SimpleNamespace(id=TICKET_ID, name="ticket-0001", created_at=NOW, overwrites={})
        category = SimpleNamespace(id=CATEGORY_ID, text_channels=[ticket])
        guild.get_channel = lambda channel_id: category if channel_id == CATEGORY_ID else None
        self.tickets_cog = FakeTicketsCog()
        self.flow_cog = FakeFlowCog("https://github.com/commit/1")
        cogs = {"AITicketsCog": self.tickets_cog, "PatreonWhitelistFlowCog": self.flow_cog}
        self.bot.get_cog = cogs.get

        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.origin = {"Origin": f"http://{self.client.host}:{self.client.port}"}
        for target in ("bulmaai.web.routes_tickets.audit", "bulmaai.web.routes_patreon.audit"):
            patcher = patch(target, AsyncMock())
            setattr(self, target.split(".")[-2] + "_audit", patcher.start())
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def post(self, path, body):
        return await self.client.post(path, json=body, headers=self.origin)

    # --- tickets ---

    async def test_helper_lists_tickets_but_cannot_toggle(self):
        self.login(HELPER_ID)
        self.tickets_cog.disabled.add(TICKET_ID)
        with patch("bulmaai.web.routes_tickets.ticket_info_for_channels", AsyncMock(return_value={})):
            data = await (await self.client.get("/api/tickets")).json()
        self.assertEqual(len(data["tickets"]), 1)
        ticket = data["tickets"][0]
        self.assertEqual(ticket["id"], str(TICKET_ID))
        self.assertFalse(ticket["ai_enabled"])
        self.assertIsNone(ticket["number"])
        self.assertEqual(ticket["url"], f"https://discord.com/channels/1/{TICKET_ID}")
        response = await self.post(f"/api/tickets/{TICKET_ID}/ai", {"enabled": True})
        self.assertEqual(response.status, 403)
        self.tickets_cog.set_ticket_ai_enabled.assert_not_called()

    async def test_tickets_list_shows_in_house_ticket_records(self):
        self.login(HELPER_ID)
        record = {
            TICKET_ID: {"ticket_id": 42, "channel_id": TICKET_ID, "category": "bug", "status": "closed",
                        "claimed_by": HELPER_ID, "owner_id": 77}
        }
        with patch("bulmaai.web.routes_tickets.ticket_info_for_channels", AsyncMock(return_value=record)):
            ticket = (await (await self.client.get("/api/tickets")).json())["tickets"][0]
        self.assertEqual((ticket["number"], ticket["category"], ticket["status"]), (42, "bug", "closed"))
        self.assertEqual(ticket["requester"], "77")
        self.assertEqual(ticket["claimed_by"]["id"], str(HELPER_ID))

    async def test_tickets_list_survives_a_ticket_table_outage(self):
        self.login(HELPER_ID)
        with patch("bulmaai.web.routes_tickets.ticket_info_for_channels", AsyncMock(side_effect=OSError("db down"))):
            with self.assertLogs("bulmaai.web.routes_tickets", "ERROR"):
                response = await self.client.get("/api/tickets")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["tickets"][0]["status"], None)

    async def test_owner_toggles_ai_through_cog(self):
        self.login(OWNER_ID)
        response = await self.post(f"/api/tickets/{TICKET_ID}/ai", {"enabled": False})
        self.assertEqual(response.status, 200, await response.text())
        self.tickets_cog.set_ticket_ai_enabled.assert_awaited_once_with(TICKET_ID, False)
        self.routes_tickets_audit.assert_awaited_once()
        self.assertEqual((await self.post("/api/tickets/123/ai", {"enabled": False})).status, 404)
        self.assertEqual((await self.post(f"/api/tickets/{TICKET_ID}/ai", {"enabled": "no"})).status, 400)

    async def test_transcript_search_and_detail(self):
        self.login(HELPER_ID)
        row = {
            "id": 7, "channel_id": TICKET_ID, "channel_name": "ticket-0001", "requester_id": 42, "closed_by_id": None,
            "resolved": True, "ai_confidence": 0.9, "message_count": 3, "title": "Crash on start", "tags": ["crash"],
            "knowledge_worthy": True, "closed_at": NOW, "guild_id": 1, "problem": "p", "resolution": "r",
            "transcript": "Transcript: #ticket-0001", "openai_file_id": None,
            "html_token": None, "html_expires_at": None,
        }
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[row]), fetchrow=AsyncMock(return_value=row))
        with patch("bulmaai.web.routes_tickets.get_pool", AsyncMock(return_value=pool)):
            data = await (await self.client.get("/api/transcripts?q=crash' OR 1=1&page=2")).json()
            query, *args = pool.fetch.await_args.args
            self.assertNotIn("1=1", query)
            self.assertEqual(args, ["crash' OR 1=1", 26, 25])
            self.assertEqual(data["transcripts"][0]["requester"], "42")
            self.assertFalse(data["has_more"])

            await self.client.get("/api/transcripts?q=42")
            self.assertEqual(pool.fetch.await_args.args[1:], (42, 26, 0))

            detail = await (await self.client.get("/api/transcripts/7")).json()
            self.assertEqual(detail["transcript"], "Transcript: #ticket-0001")
            pool.fetchrow.return_value = None
            self.assertEqual((await self.client.get("/api/transcripts/8")).status, 404)
        self.assertEqual((await self.client.get("/api/transcripts/abc")).status, 400)

    async def test_cog_toggle_changes_cog_state(self):
        cog = AITicketsCog(SimpleNamespace())
        with patch("bulmaai.cogs.ai_tickets.set_ticket_ai_disabled", AsyncMock()) as persist:
            await cog.set_ticket_ai_enabled(5, False)
            self.assertFalse(cog.is_ticket_ai_enabled(5))
            await cog.set_ticket_ai_enabled(5, True)
            self.assertTrue(cog.is_ticket_ai_enabled(5))
        self.assertEqual([c.args for c in persist.await_args_list], [(5, True), (5, False)])

    # --- patreon ---

    async def test_helper_cannot_view_patreon(self):
        self.login(HELPER_ID)
        self.assertEqual((await self.client.get("/api/patreon/grants")).status, 403)
        self.assertEqual((await self.post("/api/patreon/grant", {"user_id": "1", "minecraft_username": "Steve"})).status, 403)

    async def test_owner_grants_through_flow_cog(self):
        self.login(OWNER_ID)
        with patch("bulmaai.web.routes_patreon.list_active_grants_for_owner", AsyncMock(return_value=[])):
            bad = await self.post("/api/patreon/grant", {"user_id": str(HELPER_ID), "minecraft_username": "no spaces"})
            self.assertEqual(bad.status, 400)
            response = await self.post("/api/patreon/grant", {"user_id": str(HELPER_ID), "minecraft_username": "Steve_1"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["commit_url"], "https://github.com/commit/1")
        member = self.bot.guilds[0].get_member(HELPER_ID)
        self.flow_cog._auto_approve_beta_access.assert_awaited_once_with(member, "Steve_1")
        self.flow_cog._record_self_grant.assert_awaited_once_with(member, "Steve_1", "https://github.com/commit/1")
        self.routes_patreon_audit.assert_awaited_once()

    async def test_grant_refuses_existing_self_grant(self):
        self.login(OWNER_ID)
        existing = [grant(HELPER_ID, HELPER_ID, "OldName", PatreonGrantKind.SELF)]
        with patch("bulmaai.web.routes_patreon.list_active_grants_for_owner", AsyncMock(return_value=existing)):
            response = await self.post("/api/patreon/grant", {"user_id": str(HELPER_ID), "minecraft_username": "Steve"})
        self.assertEqual(response.status, 409)
        self.flow_cog._auto_approve_beta_access.assert_not_called()

    async def test_revoke_single_gift(self):
        self.login(OWNER_ID)
        self_grant = grant(10, 10, "Owner", PatreonGrantKind.SELF)
        gift = grant(10, 20, "Friend", PatreonGrantKind.GIFT)
        with (
            patch("bulmaai.web.routes_patreon.list_active_grants_for_owner", AsyncMock(return_value=[self_grant, gift])),
            patch("bulmaai.web.routes_patreon.deactivate_gift_grant", AsyncMock()) as deactivate_gift,
            patch("bulmaai.web.routes_patreon.deactivate_grants_for_owner", AsyncMock()) as deactivate_all,
        ):
            response = await self.post("/api/patreon/revoke", {"owner_id": "10", "beneficiary_id": "20"})
        self.assertEqual(response.status, 200, await response.text())
        deactivate_gift.assert_awaited_once_with(10, 20)
        deactivate_all.assert_not_called()
        owner_id, grants, _status = self.flow_cog._remove_whitelist_grants.await_args.args
        self.assertEqual((owner_id, grants), (10, [gift]))

    async def test_revoke_all_for_owner_reports_github_failure(self):
        self.login(OWNER_ID)
        grants = [grant(10, 10, "Owner", PatreonGrantKind.SELF)]
        self.flow_cog._remove_whitelist_grants.side_effect = RuntimeError("github down")
        with (
            patch("bulmaai.web.routes_patreon.list_active_grants_for_owner", AsyncMock(return_value=grants)),
            patch("bulmaai.web.routes_patreon.deactivate_grants_for_owner", AsyncMock(return_value=grants)) as deactivate_all,
        ):
            response = await self.post("/api/patreon/revoke", {"owner_id": "10"})
        self.assertEqual(response.status, 502)
        deactivate_all.assert_not_called()  # grants stay active so the revoke can be retried
        self.assertFalse(self.routes_patreon_audit.await_args.kwargs["github_ok"])


if __name__ == "__main__":
    unittest.main()
