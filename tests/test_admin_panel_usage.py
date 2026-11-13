import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services import ai_budget, ai_usage
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, SECRET, make_bot


class FakeConn:
    def __init__(self, store: dict) -> None:
        self.store = store

    async def fetch(self, *_args):
        return []

    async def fetchval(self, sql, *_args):
        if "ai_budget_reset" in sql:
            return self.store.get("reset_at")
        return None

    async def execute(self, sql, *args):
        if "ai_budget_reset" in sql:
            self.store["reset_at"] = args[0]


class FakeAcquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, *_exc) -> None:
        return None


class FakePool(FakeConn):
    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self)


class UsageResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store: dict = {}
        self.pool = FakePool(self.store)
        for target in ("bulmaai.web.routes_usage.get_pool", "bulmaai.web.core.get_pool"):
            patcher = patch(target, AsyncMock(return_value=self.pool))
            patcher.start()
            self.addCleanup(patcher.stop)
        flush = patch.object(ai_usage, "flush", AsyncMock(return_value=0))
        flush.start()
        self.addCleanup(flush.stop)
        ai_budget.reset()
        self.addCleanup(ai_budget.reset)
        self.client = TestClient(TestServer(create_app(make_bot())))
        await self.client.start_server()
        self.origin = {"Origin": f"http://{self.client.host}:{self.client.port}"}
        self.login(OWNER_ID)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def test_reset_zeroes_pools_stores_time_and_get_reports_it(self):
        ai_budget.record("claude-haiku-5-5", 500)
        self.assertEqual(ai_budget.used(ai_budget.SMALL), 500)
        before = await (await self.client.get("/api/usage")).json()
        self.assertIsNone(before["limits_reset_at"])
        self.assertEqual(before["today_tokens"]["small"], 500)

        response = await self.client.post("/api/usage/reset-limits", json={}, headers=self.origin)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["today_tokens"], {"small": 0, "big": 0})
        self.assertEqual(ai_budget.used(ai_budget.SMALL), 0)
        self.assertEqual(self.store["reset_at"].isoformat(), body["reset_at"])

        after = await (await self.client.get("/api/usage")).json()
        self.assertEqual(after["limits_reset_at"], body["reset_at"])
        self.assertEqual(after["today_tokens"]["small"], 0)

    async def test_reset_from_an_earlier_day_is_not_reported(self):
        self.store["reset_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
        data = await (await self.client.get("/api/usage")).json()
        self.assertIsNone(data["limits_reset_at"])

    async def test_helper_is_refused(self):
        self.login(HELPER_ID)
        ai_budget.record("claude-haiku-5-5", 500)
        response = await self.client.post("/api/usage/reset-limits", json={}, headers=self.origin)
        self.assertEqual(response.status, 403)
        self.assertEqual(ai_budget.used(ai_budget.SMALL), 500)
        self.assertNotIn("reset_at", self.store)


if __name__ == "__main__":
    unittest.main()
