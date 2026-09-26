import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.config import load_settings
from bulmaai.web import core
from bulmaai.web.core import SESSION_COOKIE, Tier, read_session, sign_session, tier_for
from bulmaai.web.server import create_app

SECRET = "test-secret"
HELPER_ROLE = 1341595261960589343
OWNER_ID = 111
HELPER_ID = 222
RANDOM_ID = 333


def make_member(user_id, guild, role_ids=(), admin=False):
    return SimpleNamespace(
        id=user_id,
        name=f"user{user_id}",
        display_name=f"User {user_id}",
        bot=False,
        guild=guild,
        roles=[SimpleNamespace(id=r) for r in role_ids],
        guild_permissions=SimpleNamespace(administrator=admin),
        display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/x.png"),
    )


def make_bot():
    settings = load_settings(include_overrides=False)
    object.__setattr__(settings, "panel_session_secret", SECRET)
    object.__setattr__(settings, "panel_public_url", "http://127.0.0.1")
    guild = SimpleNamespace(id=1, name="DMZ", icon=None, owner_id=OWNER_ID, channels=[], roles=[])
    members = {
        OWNER_ID: make_member(OWNER_ID, guild),
        HELPER_ID: make_member(HELPER_ID, guild, [HELPER_ROLE]),
        RANDOM_ID: make_member(RANDOM_ID, guild),
    }
    guild.get_member = members.get
    bot = SimpleNamespace(settings=settings, guilds=[guild], get_guild=lambda _id: guild)
    bot.reload_settings = lambda: bot.settings
    return bot


class SessionTests(unittest.TestCase):
    def test_round_trip_and_tamper(self):
        token = sign_session(SECRET, 42, now=1000)
        self.assertEqual(read_session(SECRET, token, now=1000), 42)
        self.assertIsNone(read_session("other", token, now=1000))
        self.assertIsNone(read_session(SECRET, token[:-1] + "0", now=1000))
        self.assertIsNone(read_session(SECRET, token, now=1000 + core.SESSION_TTL_SECONDS + 1))
        self.assertIsNone(read_session(SECRET, None))

    def test_tiers(self):
        bot = make_bot()
        guild = bot.guilds[0]
        self.assertEqual(tier_for(guild.get_member(OWNER_ID), bot.settings), Tier.OWNER)
        self.assertEqual(tier_for(guild.get_member(HELPER_ID), bot.settings), Tier.HELPER)
        self.assertEqual(tier_for(guild.get_member(RANDOM_ID), bot.settings), Tier.NONE)
        self.assertEqual(tier_for(make_member(9, guild, admin=True), bot.settings), Tier.ADMIN)


class PanelApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        audit_patch = patch("bulmaai.web.core.get_pool", AsyncMock(return_value=SimpleNamespace(execute=AsyncMock())))
        audit_patch.start()
        self.addCleanup(audit_patch.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def test_requires_login(self):
        response = await self.client.get("/api/me")
        self.assertEqual(response.status, 401)

    async def test_non_staff_is_rejected(self):
        self.login(RANDOM_ID)
        response = await self.client.get("/api/me")
        self.assertEqual(response.status, 401)

    async def test_helper_sees_own_permissions_only(self):
        self.login(HELPER_ID)
        data = await (await self.client.get("/api/me")).json()
        self.assertEqual(data["tier"], "helper")
        self.assertIn("mod.warn", data["permissions"])
        self.assertNotIn("settings.edit", data["permissions"])
        self.assertEqual((await self.client.get("/api/settings")).status, 403)

    async def test_cross_origin_write_is_rejected(self):
        self.login(OWNER_ID)
        response = await self.client.put(
            "/api/settings/showcase_threshold", json={"value": "7"}, headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(response.status, 403)

    async def test_owner_updates_setting(self):
        self.login(OWNER_ID)
        origin = f"http://{self.client.host}:{self.client.port}"
        with patch("bulmaai.web.routes_settings.set_setting_override", return_value=7) as setter:
            response = await self.client.put(
                "/api/settings/showcase_threshold", json={"value": "7"}, headers={"Origin": origin}
            )
        self.assertEqual(response.status, 200, await response.text())
        setter.assert_called_once_with("showcase_threshold", "7")

    async def test_public_url_origin_is_accepted_when_host_is_rewritten(self):
        self.login(OWNER_ID)
        object.__setattr__(self.bot.settings, "panel_public_url", "https://panel.example")
        with patch("bulmaai.web.routes_settings.set_setting_override", return_value=7):
            response = await self.client.put(
                "/api/settings/showcase_threshold", json={"value": "7"}, headers={"Origin": "https://panel.example"}
            )
        self.assertEqual(response.status, 200, await response.text())

    async def test_security_headers(self):
        response = await self.client.get("/")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
