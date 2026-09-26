import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services.message_presets import DEFAULT_MESSAGE_PRESETS
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, SECRET, make_bot


class PresetsPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.presets_file = Path(tmp.name) / "message_presets.json"
        for target, kwargs in (
            ("bulmaai.services.message_presets._presets_path", {"return_value": self.presets_file}),
            ("bulmaai.web.core.get_pool", {"new": AsyncMock(return_value=None)}),
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

    async def test_helper_is_denied(self):
        self.login(HELPER_ID)
        self.assertEqual((await self.client.get("/api/presets")).status, 403)

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


if __name__ == "__main__":
    unittest.main()
