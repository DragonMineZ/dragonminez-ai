import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services.support_info import SupportInfo
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, RANDOM_ID, SECRET, make_bot

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def entry(info_id=1, status="active", **overrides):
    values = dict(
        id=info_id, kind="temporary", title="Crash", body="Use v2.2.1", status=status, source="manual", ticket_id=None,
        expires_at=None, created_by=OWNER_ID, updated_by=OWNER_ID, review_channel_id=None, review_message_id=None,
        created_at=NOW, updated_at=NOW,
    )
    values.update(overrides)
    return SupportInfo(**values)


class SupportInfoPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        self.cog = SimpleNamespace(finish_suggestion=AsyncMock(return_value=entry(status="active")))
        self.bot.get_cog = lambda name: self.cog if name == "SupportInfoCog" else None
        patcher = patch("bulmaai.web.core.get_pool", AsyncMock(return_value=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.audit = patch("bulmaai.web.routes_support_info.audit", AsyncMock()).start()
        self.addCleanup(patch.stopall)
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.origin = {"Origin": f"http://{self.client.host}:{self.client.port}"}
        self.login(HELPER_ID)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def post(self, path, body=None, method="POST"):
        return await self.client.request(method, path, json=body or {}, headers=self.origin)

    async def test_list_by_status(self):
        rows = {"removed": [entry(2, "removed")], "discarded": [entry(3, "discarded")], "active": [entry(1)]}
        fake = AsyncMock(side_effect=lambda status: rows.get(status, []))
        with patch("bulmaai.web.routes_support_info.support_info.list_by_status", fake):
            active = await (await self.client.get("/api/support-info")).json()
            closed = await (await self.client.get("/api/support-info?status=closed")).json()
            bad = await self.client.get("/api/support-info?status=nope")
        self.assertEqual([e["id"] for e in active["entries"]], [1])
        self.assertEqual({e["id"] for e in closed["entries"]}, {2, 3})
        self.assertEqual(bad.status, 400)

    async def test_create_validation_error_is_400(self):
        response = await self.post("/api/support-info", {"kind": "temporary", "title": "x" * 101, "body": "text"})
        self.assertEqual(response.status, 400)
        self.assertIn("too long", (await response.json())["error"])
        self.audit.assert_not_awaited()

    async def test_create_rejects_past_expiry(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        response = await self.post("/api/support-info", {"kind": "temporary", "title": "t", "body": "b", "expires_at": past})
        self.assertEqual(response.status, 400)

    async def test_create_audits(self):
        add = AsyncMock(return_value=entry(7))
        with patch("bulmaai.web.routes_support_info.support_info.add_info", add):
            response = await self.post("/api/support-info", {"kind": "permanent", "title": " t ", "body": "b"})
        self.assertEqual(response.status, 200)
        self.assertEqual(add.await_args.kwargs["title"], "t")
        self.assertEqual(add.await_args.kwargs["created_by"], HELPER_ID)
        self.assertEqual(self.audit.await_args.args[1], "support_info.create")

    async def test_approve_calls_cog_and_409_when_handled(self):
        response = await self.post("/api/support-info/5/approve")
        self.assertEqual(response.status, 200)
        self.cog.finish_suggestion.assert_awaited_once_with(5, status="active", by_id=HELPER_ID)
        self.cog.finish_suggestion.return_value = None
        response = await self.post("/api/support-info/5/discard")
        self.assertEqual(response.status, 409)
        self.assertEqual(self.cog.finish_suggestion.await_args.kwargs["status"], "discarded")

    async def test_approve_without_cog_is_503(self):
        self.bot.get_cog = lambda name: None
        self.assertEqual((await self.post("/api/support-info/5/approve")).status, 503)

    async def test_remove_and_already_removed(self):
        with patch("bulmaai.web.routes_support_info.support_info.set_status", AsyncMock(return_value=entry(1, "removed"))):
            self.assertEqual((await self.post("/api/support-info/1/remove")).status, 200)
        with patch("bulmaai.web.routes_support_info.support_info.set_status", AsyncMock(return_value=None)):
            self.assertEqual((await self.post("/api/support-info/1/remove")).status, 409)

    async def test_update_missing_is_404(self):
        with (
            patch("bulmaai.web.routes_support_info.support_info.get_info", AsyncMock(return_value=None)),
            patch("bulmaai.web.routes_support_info.support_info.update_info", AsyncMock(return_value=None)),
        ):
            response = await self.post("/api/support-info/9", {"kind": "permanent", "title": "t", "body": "b"}, method="PUT")
        self.assertEqual(response.status, 404)

    async def test_non_staff_is_denied(self):
        self.login(RANDOM_ID)
        self.assertEqual((await self.client.get("/api/support-info")).status, 401)
        self.assertEqual((await self.post("/api/support-info/1/approve")).status, 401)
        self.cog.finish_suggestion.assert_not_awaited()


class SupportInfoTierTests(unittest.TestCase):
    def test_helpers_have_the_permission(self):
        from bulmaai.web.core import PERMISSIONS, Tier

        self.assertEqual(PERMISSIONS["support_info.edit"], Tier.HELPER)
