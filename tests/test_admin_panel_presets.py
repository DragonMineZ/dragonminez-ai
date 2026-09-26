import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services.message_presets import DEFAULT_MESSAGE_PRESETS
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, SECRET, make_bot

BOT_ID = 999
CHANNEL_ID = 10


def perms(**overrides):
    values = dict(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
    values.update(overrides)
    return SimpleNamespace(**values)


def make_message(author_id=BOT_ID, content="old", embeds=()):
    return SimpleNamespace(
        id=555,
        author=SimpleNamespace(id=author_id),
        content=content,
        embeds=list(embeds),
        jump_url=f"https://discord.com/channels/1/{CHANNEL_ID}/555",
        edit=AsyncMock(),
    )


class PresetsPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        self.bot.user = SimpleNamespace(id=BOT_ID)
        self.guild = self.bot.guilds[0]
        self.guild.me = SimpleNamespace(id=BOT_ID)
        self.perms = perms()
        self.channel = SimpleNamespace(
            id=CHANNEL_ID,
            name="announcements",
            type="news",
            permissions_for=lambda _member: self.perms,
            send=AsyncMock(return_value=make_message()),
            fetch_message=AsyncMock(return_value=make_message()),
        )
        voice = SimpleNamespace(id=11, name="voice", type="voice")
        self.guild.get_channel = {CHANNEL_ID: self.channel, 11: voice}.get

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.presets_file = Path(tmp.name) / "message_presets.json"
        for target, kwargs in (
            ("bulmaai.services.message_presets._presets_path", {"return_value": self.presets_file}),
            ("bulmaai.web.core.get_pool", {"new": AsyncMock(return_value=SimpleNamespace(execute=AsyncMock()))}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.audit = patch("bulmaai.web.routes_presets.audit", AsyncMock()).start()
        self.addCleanup(patch.stopall)

        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.origin = {"Origin": f"http://{self.client.host}:{self.client.port}"}
        self.login(OWNER_ID)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def write(self, method, path, body):
        return await self.client.request(method, path, json=body, headers=self.origin)

    # ---------- presets ----------

    async def test_helper_is_denied(self):
        self.login(HELPER_ID)
        self.assertEqual((await self.client.get("/api/presets")).status, 403)
        self.assertEqual((await self.write("POST", "/api/announce", {"content": "hi"})).status, 403)

    async def test_list_renders_all_default_presets(self):
        data = await (await self.client.get("/api/presets")).json()
        self.assertEqual(len(data["presets"]), 6)
        rules_en = next(p for p in data["presets"] if p["kind"] == "rules" and p["language"] == "en")
        self.assertFalse(rules_en["customized"])
        self.assertEqual(len(rules_en["embeds"]), len(DEFAULT_MESSAGE_PRESETS["rules"]["en"]["sections"]))

    async def test_save_rules_preset(self):
        body = {"data": {"title": "Rules", "sections": [{"title": None, "content": "Be nice"}, {"title": "1. X", "content": "Y"}]}}
        response = await self.write("PUT", "/api/presets/rules/es", body)
        self.assertEqual(response.status, 200, await response.text())
        saved = json.loads(self.presets_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["rules"]["es"], body["data"])
        self.assertEqual(saved["rules"]["en"], DEFAULT_MESSAGE_PRESETS["rules"]["en"])
        self.assertTrue((await response.json())["customized"])
        self.assertEqual(self.audit.await_args.args[1:3], ("presets.update", "rules/es"))

        reset = await self.write("DELETE", "/api/presets/rules/es", None)
        self.assertEqual(reset.status, 200)
        self.assertEqual(json.loads(self.presets_file.read_text(encoding="utf-8"))["rules"]["es"], DEFAULT_MESSAGE_PRESETS["rules"]["es"])

    async def test_preset_validation(self):
        too_long_field = {"title": "R", "sections": [{"title": "Heading", "content": "x" * 1025}]}
        response = await self.write("PUT", "/api/presets/rules/en", {"data": too_long_field})
        self.assertEqual(response.status, 400)
        self.assertIn("1024", (await response.json())["error"])
        self.assertFalse(self.presets_file.exists())

        # Without a heading the same text becomes the description, which allows 4096.
        as_description = {"title": "R", "sections": [{"title": None, "content": "x" * 1025}]}
        self.assertEqual((await self.write("POST", "/api/presets/rules/en/preview", {"data": as_description})).status, 200)

        support = dict(DEFAULT_MESSAGE_PRESETS["support"]["en"])
        del support["github_label"]
        self.assertEqual((await self.write("PUT", "/api/presets/support/en", {"data": support})).status, 400)
        support = dict(DEFAULT_MESSAGE_PRESETS["support"]["en"], patreon_label="x" * 81)
        self.assertEqual((await self.write("PUT", "/api/presets/support/en", {"data": support})).status, 400)
        self.assertEqual((await self.write("PUT", "/api/presets/rules/fr", {"data": as_description})).status, 404)

    # ---------- announcements ----------

    async def test_send_defaults_to_no_pings(self):
        body = {"channel_id": str(CHANNEL_ID), "content": "Hello @everyone", "embed": {"title": "Update", "color": "#f39c12"}}
        response = await self.write("POST", "/api/announce", body)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["jump_url"], make_message().jump_url)
        kwargs = self.channel.send.await_args.kwargs
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertEqual(kwargs["embed"].colour.value, 0xF39C12)
        self.audit.assert_awaited_once()
        self.assertEqual(self.audit.await_args.kwargs["message_id"], "555")

        await self.write("POST", "/api/announce", dict(body, allow_pings=True))
        self.assertEqual(self.channel.send.await_args.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.all().to_dict())

    async def test_send_validation(self):
        cases = [
            {"channel_id": str(CHANNEL_ID)},
            {"channel_id": str(CHANNEL_ID), "embed": {"footer": "only a footer"}},
            {"channel_id": str(CHANNEL_ID), "content": "x" * 2001},
            {"channel_id": str(CHANNEL_ID), "content": "hi", "embed": {"title": "t", "color": "orange"}},
            {"channel_id": str(CHANNEL_ID), "content": "hi", "embed": {"title": "t", "image_url": "javascript:alert(1)"}},
            {"channel_id": str(CHANNEL_ID), "embed": {"description": "x" * 4000, "footer": "y" * 2048}},
            {"channel_id": "11", "content": "hi"},
            {"channel_id": "12345", "content": "hi"},
            {"channel_id": str(CHANNEL_ID), "content": "hi", "allow_pings": "yes"},
        ]
        for body in cases:
            with self.subTest(body=str(body)[:80]):
                self.assertEqual((await self.write("POST", "/api/announce", body)).status, 400)
        self.channel.send.assert_not_awaited()

        self.perms = perms(send_messages=False)
        response = await self.write("POST", "/api/announce", {"channel_id": str(CHANNEL_ID), "content": "hi"})
        self.assertEqual(response.status, 403)
        self.assertIn("Send Messages", (await response.json())["error"])

    async def test_load_and_edit_own_message(self):
        embed = discord.Embed(title="Old", description="Body", color=0x112233)
        embed.set_footer(text="foot")
        message = make_message(embeds=[embed])
        self.channel.fetch_message.return_value = message

        link = f"https://discord.com/channels/1/{CHANNEL_ID}/555"
        data = await (await self.client.get("/api/announce/message", params={"ref": link})).json()
        self.assertEqual(data["embed"]["color"], "#112233")
        self.assertEqual(data["embed"]["footer"], "foot")
        self.assertEqual(data["content"], "old")

        body = {"content": "", "embed": dict(data["embed"], title="New")}
        response = await self.write("PATCH", f"/api/announce/{CHANNEL_ID}/555", body)
        self.assertEqual(response.status, 200, await response.text())
        kwargs = message.edit.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"][0].title, "New")
        self.assertEqual(self.audit.await_args.args[1], "announce.edit")

    async def test_edit_rejections(self):
        other_guild = f"https://discord.com/channels/2/{CHANNEL_ID}/555"
        self.assertEqual((await self.client.get("/api/announce/message", params={"ref": other_guild})).status, 400)

        self.channel.fetch_message.return_value = make_message(author_id=123)
        response = await self.client.get("/api/announce/message", params={"ref": "555", "channel_id": str(CHANNEL_ID)})
        self.assertEqual(response.status, 403)
        edit = await self.write("PATCH", f"/api/announce/{CHANNEL_ID}/555", {"content": "hijack"})
        self.assertEqual(edit.status, 403)

        with_fields = discord.Embed(title="Rules")
        with_fields.add_field(name="1", value="x")
        self.channel.fetch_message.return_value = make_message(embeds=[with_fields])
        response = await self.client.get("/api/announce/message", params={"ref": "555", "channel_id": str(CHANNEL_ID)})
        self.assertEqual(response.status, 409)


if __name__ == "__main__":
    unittest.main()
