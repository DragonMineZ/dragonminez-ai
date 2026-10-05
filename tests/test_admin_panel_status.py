import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from aiohttp.test_utils import TestClient, TestServer

from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, RANDOM_ID, SECRET, make_bot, make_member

MOD_ROLE = 1352882775304175668
MOD_ID = 444


def make_status_bot():
    bot = make_bot()
    guild = bot.guilds[0]
    guild.member_count = 3
    members = {m.id: m for m in (guild.get_member(i) for i in (OWNER_ID, HELPER_ID, RANDOM_ID))}
    members[MOD_ID] = make_member(MOD_ID, guild, [MOD_ROLE])
    guild.get_member = members.get
    guild.members = list(members.values())
    bot.user = make_member(999, guild)
    bot.latency = 0.0421
    bot.extensions = {"bulmaai.cogs.meta": None, "bulmaai.cogs.admin_panel": None}
    bot.reload_extension = MagicMock()
    return bot


class StatusApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_status_bot()
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.pool = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(return_value=1), fetch=AsyncMock(return_value=[]))
        for target in ("bulmaai.web.core.get_pool", "bulmaai.web.routes_status.get_pool"):
            p = patch(target, AsyncMock(return_value=self.pool))
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    def post(self, path):
        return self.client.post(path, headers={"Origin": f"http://{self.client.host}:{self.client.port}"})

    async def test_status_for_helper(self):
        self.login(HELPER_ID)
        response = await self.client.get("/api/status")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(data["latency_ms"], 42)
        self.assertTrue(data["db"]["ok"])
        self.assertEqual(data["guild"]["member_count"], 3)
        self.assertEqual({p["pool"] for p in data["ai_budget"]["pools"]}, {"small", "big", "billed"})
        self.assertIn("bulmaai.cogs.meta", data["extensions"])

    async def test_status_reports_db_error(self):
        self.pool.fetchval.side_effect = OSError("connection refused")
        self.login(HELPER_ID)
        data = await (await self.client.get("/api/status")).json()
        self.assertFalse(data["db"]["ok"])
        self.assertIn("connection refused", data["db"]["error"])

    async def test_reload_permissions_and_guards(self):
        self.login(HELPER_ID)
        self.assertEqual((await self.post("/api/status/extensions/bulmaai.cogs.meta/reload")).status, 403)
        self.login(OWNER_ID)
        self.assertEqual((await self.post("/api/status/extensions/bulmaai.cogs.nope/reload")).status, 404)
        self.assertEqual((await self.post("/api/status/extensions/bulmaai.cogs.admin_panel/reload")).status, 400)
        self.bot.reload_extension.assert_not_called()

    async def test_reload_happy_path_and_failure(self):
        self.login(OWNER_ID)
        response = await self.post("/api/status/extensions/bulmaai.cogs.meta/reload")
        self.assertEqual(response.status, 200, await response.text())
        self.bot.reload_extension.assert_called_once_with("bulmaai.cogs.meta")
        self.assertEqual(self.pool.execute.await_args.args[2], "bot.reload")

        self.bot.reload_extension.side_effect = discord.ExtensionFailed("bulmaai.cogs.meta", RuntimeError("bad"))
        with self.assertLogs("bulmaai.web.routes_status", "ERROR"):
            self.assertEqual((await self.post("/api/status/extensions/bulmaai.cogs.meta/reload")).status, 502)

    async def test_audit_query_filters_and_actor_resolution(self):
        self.pool.fetch.return_value = [
            {
                "id": 10 - i,
                "actor_id": actor,
                "action": "settings.set",
                "target": "x",
                "details": '{"after": 7}',
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }
            for i, actor in enumerate((OWNER_ID, 5555, OWNER_ID))
        ]
        self.login(MOD_ID)
        self.assertEqual((await self.client.get("/api/audit")).status, 403)
        self.login(OWNER_ID)
        # A non-digit actor_id is now a name search (see test_admin_panel_logs.py), not an error.
        self.assertEqual((await self.client.get("/api/audit?actor_id=nobody-like-this")).status, 200)
        response = await self.client.get(f"/api/audit?limit=2&before_id=11&actor_id={OWNER_ID}&action=settings.")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertTrue(data["has_more"])
        self.assertEqual(len(data["entries"]), 2)
        self.assertEqual(data["entries"][0]["actor"]["id"], str(OWNER_ID))
        self.assertEqual(data["entries"][1]["actor"], "5555")
        self.assertEqual(data["entries"][0]["details"], {"after": 7})
        sql, *args = self.pool.fetch.await_args.args
        self.assertIn("id < $1", sql)
        self.assertIn("actor_id = $2", sql)
        self.assertEqual(args, [11, OWNER_ID, "settings.", 3])

    async def test_staff_lists_tiers(self):
        self.login(OWNER_ID)
        data = await (await self.client.get("/api/staff")).json()
        tiers = {m["user"]["id"]: m["tier"] for m in data["members"]}
        self.assertEqual(tiers, {str(OWNER_ID): "owner", str(MOD_ID): "moderator", str(HELPER_ID): "helper"})
        self.assertEqual(data["members"][0]["tier"], "owner")
        self.assertIn({"name": "bot.reload", "tier": "admin", "tier_level": 3}, data["permissions"])
        self.assertEqual(data["roles"]["owner"], [{"id": "1216431257660035132", "name": None}])
        helper_roles = {r["id"]: r["name"] for r in data["roles"]["helper"]}
        self.assertEqual(helper_roles, {"1341595261960589343": "DMZ Helper", "1341596685339725885": None})


if __name__ == "__main__":
    unittest.main()
