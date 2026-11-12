import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.web import routes_mcp
from bulmaai.web.server import create_app
from tests.test_admin_panel import HELPER_ID, OWNER_ID, RANDOM_ID, make_bot

TOKENS = {"helper-token": HELPER_ID, "owner-token": OWNER_ID, "random-token": RANDOM_ID}
AUTH = {"Authorization": "Bearer helper-token"}


def rpc(method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    return body


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        self.cog = SimpleNamespace(send_confirm_dm=AsyncMock(return_value=True))
        self.bot.get_cog = lambda name: self.cog if name == "StaffAssistantCog" else None
        self.profiles = {HELPER_ID: {"language": "en"}, OWNER_ID: {"language": "en"}}
        self.run_tool = AsyncMock(return_value={"ok": True})
        self.cancel = AsyncMock(return_value=True)
        routes_mcp._calls.clear()
        for target, new in (
            ("bulmaai.services.staff_store.user_for_token", AsyncMock(side_effect=lambda t: TOKENS.get(t))),
            ("bulmaai.services.staff_store.get_profile", AsyncMock(side_effect=lambda u: self.profiles.get(u))),
            ("bulmaai.services.staff_store.cancel_action", self.cancel),
            ("bulmaai.services.staff_tools.run_tool", self.run_tool),
            ("bulmaai.web.core.get_pool", AsyncMock(return_value=SimpleNamespace(execute=AsyncMock()))),
        ):
            patcher = patch(target, new)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def post(self, body, headers=AUTH):
        return await self.client.post("/mcp", json=body, headers=headers)

    async def test_auth_failures(self):
        for headers in ({"Authorization": "Bearer "}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer random-token"}):
            response = await self.post(rpc("ping"), headers)
            self.assertEqual(response.status, 401)
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    async def test_no_profile_is_rejected(self):
        self.profiles.pop(HELPER_ID)
        self.assertEqual((await self.post(rpc("ping"))).status, 401)

    async def test_disabled_is_rejected(self):
        object.__setattr__(self.bot.settings, "staff_ai_enabled", False)
        self.assertEqual((await self.post(rpc("ping"))).status, 401)

    async def test_initialize_negotiates_version(self):
        data = await (await self.post(rpc("initialize", {"protocolVersion": "2025-03-26"}))).json()
        self.assertEqual(data["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(data["result"]["serverInfo"]["name"], "bulma")
        data = await (await self.post(rpc("initialize", {"protocolVersion": "1999-01-01"}))).json()
        self.assertEqual(data["result"]["protocolVersion"], "2025-06-18")

    async def test_notification_and_ping(self):
        response = await self.post(rpc("notifications/initialized", request_id=None))
        self.assertEqual(response.status, 202)
        self.assertEqual(await response.read(), b"")
        self.assertEqual((await (await self.post(rpc("ping"))).json())["result"], {})

    async def test_batch_and_methods(self):
        self.assertEqual((await self.post([rpc("ping")])).status, 400)
        self.assertEqual((await self.client.get("/mcp", headers=AUTH)).status, 405)
        self.assertEqual((await self.client.delete("/mcp", headers={**AUTH, "Origin": "http://127.0.0.1"})).status, 405)
        data = await (await self.post(rpc("nope"))).json()
        self.assertEqual(data["error"]["code"], -32601)
        data = await (await self.post(rpc("tools/call", {"arguments": {}}))).json()
        self.assertEqual(data["error"]["code"], -32602)
        response = await self.client.post("/mcp", data="{bad", headers=AUTH)
        self.assertEqual((await response.json())["error"]["code"], -32700)

    async def test_tools_list_follows_tier(self):
        helper = {t["name"]: t for t in (await (await self.post(rpc("tools/list"))).json())["result"]["tools"]}
        self.assertIn("member_lookup", helper)
        self.assertNotIn("ban", helper)
        self.assertIn("inputSchema", helper["member_lookup"])
        self.assertTrue(helper["warn"]["description"].endswith("confirms it in Discord DMs."))
        self.assertNotIn("Only proposes", helper["member_lookup"]["description"])
        owner = await (await self.post(rpc("tools/list"), {"Authorization": "Bearer owner-token"})).json()
        self.assertIn("ban", {t["name"] for t in owner["result"]["tools"]})

    async def test_lookup_call(self):
        self.run_tool.return_value = {"member": "x"}
        data = await (await self.post(rpc("tools/call", {"name": "member_lookup", "arguments": {"user": "1"}}))).json()
        result = data["result"]
        self.assertFalse(result["isError"])
        self.assertIn('"member"', result["content"][0]["text"])
        ctx = self.run_tool.await_args.args[2]
        self.assertEqual(ctx.origin, "mcp")
        self.assertIsNone(ctx.channel_id)

    async def test_error_result_flagged(self):
        self.run_tool.return_value = {"error": "nope"}
        data = await (await self.post(rpc("tools/call", {"name": "ban", "arguments": {}}))).json()
        self.assertTrue(data["result"]["isError"])

    async def test_action_sends_confirm_dm(self):
        self.run_tool.return_value = {"pending_action": 7, "summary": "Warn Bob"}
        data = await (await self.post(rpc("tools/call", {"name": "warn", "arguments": {"user": "5"}}))).json()
        self.cog.send_confirm_dm.assert_awaited_once()
        self.assertEqual(self.cog.send_confirm_dm.await_args.args[1], 7)
        self.assertFalse(data["result"]["isError"])
        self.assertIn("Proposed: Warn Bob.", data["result"]["content"][0]["text"])
        self.cancel.assert_not_awaited()

    async def test_action_dm_failure_cancels(self):
        self.cog.send_confirm_dm.return_value = False
        self.run_tool.return_value = {"pending_action": 7, "summary": "Warn Bob"}
        data = await (await self.post(rpc("tools/call", {"name": "warn", "arguments": {"user": "5"}}))).json()
        self.cancel.assert_awaited_once_with(7, HELPER_ID)
        self.assertTrue(data["result"]["isError"])
        self.assertIn("DM you", data["result"]["content"][0]["text"])

    async def test_origin_rules(self):
        foreign = await self.client.post(
            "/mcp", json=rpc("ping"), headers={**AUTH, "Origin": "https://evil.example"}
        )
        self.assertEqual(foreign.status, 403)
        own = await self.client.post("/mcp", json=rpc("ping"), headers={**AUTH, "Origin": "http://127.0.0.1"})
        self.assertEqual(own.status, 200)
        self.assertEqual((await self.post(rpc("ping"))).status, 200)
        no_bearer = await self.client.post("/mcp", json=rpc("ping"))
        self.assertEqual(no_bearer.status, 403)
        other = await self.client.post("/api/me", json={}, headers=AUTH)
        self.assertEqual(other.status, 403)

    async def test_rate_limit(self):
        for _ in range(routes_mcp.RATE_LIMIT):
            self.assertNotIn("error", await (await self.post(rpc("ping"))).json())
        data = await (await self.post(rpc("ping"))).json()
        self.assertEqual(data["error"]["code"], -32000)


if __name__ == "__main__":
    unittest.main()
