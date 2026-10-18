import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services import log_pages
from bulmaai.web.server import create_app
from test_admin_panel import make_bot

TOKEN = "l" * 32
LOG = """[19:07:39] [main/INFO] [cp.mo.mo.Launcher/MODLAUNCHER]: ModLauncher running: args [--username, Goku123, --gameDir, C:\\Users\\Bruno Silva\\curseforge\\Instance]
[19:07:40] [Render thread/ERROR] [dragonminez/]: Boom <script>alert(1)</script>
java.lang.IllegalStateException: Boom
\tat com.dragonminez.Foo.bar(Foo.java:10)
[19:07:41] [Render thread/WARN] [minecraft/]: Connecting to play.example.net, 25565
[19:07:42] [Server thread/INFO] [minecraft/]: Goku[/203.0.113.7:51234] logged in, mod 1.20.1.2 loaded
"""


class RedactAndRenderTests(unittest.TestCase):
    def test_redacts_account_paths_and_ips_but_not_versions(self):
        text = log_pages.redact(LOG + "/home/goku/.minecraft and C:/Users/goku/AppData\n")
        for secret in ("Goku123", "Bruno Silva", "play.example.net", "203.0.113.7", "/home/goku", "Users/goku"):
            self.assertNotIn(secret, text)
        self.assertIn("C:\\Users\\<user>\\curseforge", text)
        self.assertIn("mod 1.20.1.2 loaded", text)

    def test_player_name_is_learned_from_the_log_and_masked_everywhere(self):
        log = (
            "[12:00:00] [main/INFO]: Setting user: SonGoku99\n"
            "[12:00:01] [Server thread/INFO]: SonGoku99[/127.0.0.1:5000] logged in with entity id 7\n"
            "[12:00:02] [Server thread/INFO]: SonGoku99 joined the game\n"
            "[12:00:03] [Render thread/INFO]: [CHAT] <SonGoku99> hi, SonGoku990 is my alt\n"
            "Saved to C:\\Users\\Someone\\saves\\SonGoku99_world\n"
        )
        self.assertEqual(log_pages.player_names(log), {"SonGoku99"})
        text = log_pages.redact(log)
        self.assertNotIn("SonGoku99 ", text)
        self.assertIn("Setting user: <player>", text)
        self.assertIn("<player> joined the game", text)
        self.assertIn("<<player>>", text)
        self.assertIn("SonGoku990 is my alt", text)  # only the whole name, not a longer word containing it
        self.assertIn("<ip>", text)

    def test_generic_names_are_never_learned(self):
        self.assertEqual(log_pages.player_names("Setting user: Player\n[x]: Dev joined the game\n"), set())

    def test_extra_names_are_masked_in_other_text(self):
        self.assertEqual(log_pages.redact("thanks Vegeta77, still broken", names={"Vegeta77"}), "thanks <player>, still broken")

    def test_emails_tokens_jwts_and_player_uuids_are_masked(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123_-"
        text = log_pages.redact(
            "contact me at goku.son@capsule-corp.example.com\n"
            f"--accessToken {jwt}\n"
            f"session token:{jwt}:0a1b\n"
            'accessToken="abc123secret" sessionId: s3cr3t\n'
            f"bearer {jwt}\n"
            "UUID of player Goku is 0a1b2c3d-1111-2222-3333-444455556666\n"
            "player_uuid=0a1b2c3d111122223333444455556666\n"
            "Registered capability with id 9f8e7d6c-1111-2222-3333-444455556666\n"
        )
        for secret in ("goku.son@", "capsule-corp", "eyJhbGci", "abc123secret", "s3cr3t", "0a1b2c3d"):
            self.assertNotIn(secret, text)
        self.assertIn("<email>", text)
        self.assertIn("is <uuid>", text)
        self.assertIn("player_uuid=<uuid>", text)
        self.assertIn("9f8e7d6c-1111-2222-3333-444455556666", text)  # not a player's id

    def test_log_page_escapes_colors_levels_and_lists_problems(self):
        page = log_pages.render(LOG, title="latest.log", kind="log", facts=[("Forge", "47.4.10")], marks=[(2, "Boom")])
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn('class="r l-ERROR" id="L2"', page)
        self.assertIn('class="r l-ERROR tr" id="L4"', page)  # the frame stays with its error when filtering
        self.assertIn('class="r l-WARN" id="L5"', page)
        self.assertIn('<a class="err" href="#L2"><code>2</code> Boom</a>', page)
        self.assertIn('data-level="DEBUG"', page)
        self.assertIn("<b>Forge</b> 47.4.10", page)

    def test_dmzdebug_page_lists_sections_and_colors_pairs(self):
        page = log_pages.render("## DMZ DEBUG ##\n\n# Stats:\nSTR: 5\n\n# Status:\nIsAlive: 1b\n", title="Goku.log", kind="dmzdebug")
        self.assertIn('<a class="sec" href="#L3"><code>3</code> Stats</a>', page)
        self.assertIn('<a class="sec" href="#L6"><code>6</code> Status</a>', page)
        self.assertIn('<i class="k">IsAlive</i>: <i class="v on">1b</i>', page)
        self.assertNotIn('type="checkbox"', page)  # no level filters on a dump

    def test_json_dmzdebug_page_lists_blocks_and_sections(self):
        dump = json.dumps({"schemaVersion": 1, "player": {"name": "Goku", "online": True},
                           "sections": {"Stats": {"STR": 5}, "Status": {"IsAlive": 1}}}, indent=2)
        page = log_pages.render(dump, title="Goku.json", kind="dmzdebug")
        self.assertIn('<code>3</code> player</a>', page)
        self.assertIn('<code>8</code> Stats</a>', page)
        self.assertIn('<code>11</code> Status</a>', page)
        self.assertNotIn("</code> sections</a>", page)
        self.assertIn('<i class="k">online</i>": <i class="v on">true</i>', page)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = SimpleNamespace(log_page_dir=self._dir.name, log_page_public_url="https://logs.example/")

    async def test_save_serves_for_24h_then_purges(self):
        url = await log_pages.save(self.settings, "<p>hi</p>")
        token = url.rsplit("/", 1)[1]
        self.assertEqual(url, f"https://logs.example/l/{token}")
        self.assertEqual(log_pages.read_live(self.settings, token), b"<p>hi</p>")
        later = time.time() + log_pages.EXPIRY_SECONDS + 1
        self.assertIsNone(log_pages.read_live(self.settings, token, now=later))
        self.assertEqual(log_pages.purge(self.settings), 0)
        self.assertEqual(log_pages.purge(self.settings, now=later), 1)
        self.assertIsNone(log_pages.read_live(self.settings, token))

    def test_bad_tokens_never_touch_the_filesystem(self):
        for bad in ("../../etc/passwd", "short", "a" * 33):
            self.assertIsNone(log_pages.read_live(self.settings, bad))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.bot = make_bot()
        self.bot.get_cog = lambda name: None
        object.__setattr__(self.bot.settings, "log_page_dir", self._dir.name)
        (Path(self._dir.name) / f"{TOKEN}.html").write_text("<html>the log</html>", encoding="utf-8")
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_live_page_is_served_sandboxed_without_login(self):
        response = await self.client.get(f"/l/{TOKEN}")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "<html>the log</html>")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("sandbox allow-scripts", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertEqual((await self.client.get(f"/l/{'m' * 32}")).status, 404)

    async def test_logs_hostname_serves_only_log_pages(self):
        object.__setattr__(self.bot.settings, "log_page_public_url", "https://logs.example")
        headers = {"Host": "logs.example"}
        self.assertEqual((await self.client.get(f"/l/{TOKEN}", headers=headers)).status, 200)
        self.assertEqual((await self.client.get("/api/me", headers=headers)).status, 404)
        self.assertEqual((await self.client.get(f"/t/{TOKEN}", headers=headers)).status, 404)


if __name__ == "__main__":
    unittest.main()
