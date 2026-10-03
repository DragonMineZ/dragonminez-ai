import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services import ticket_pages
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, SECRET, make_bot

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
TOKEN = "t" * 32


class PageStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = SimpleNamespace(
            ticket_transcript_dir=self._dir.name,
            ticket_transcript_public_url="https://tickets.example/",
            ticket_transcript_retention_days=30,
        )

    def test_tokens_are_long_and_paths_cannot_escape(self):
        token = ticket_pages.new_token()
        self.assertRegex(token, ticket_pages.TOKEN_PATTERN)
        self.assertEqual(ticket_pages.page_url(self.settings, token), f"https://tickets.example/t/{token}")
        for bad in ("../../etc/passwd", "short", "a" * 33, ""):
            with self.assertRaises(ValueError):
                ticket_pages.page_path(self.settings, bad)

    async def test_save_page_writes_the_file_and_sets_the_expiry(self):
        page = await ticket_pages.save_page(self.settings, b"<html>hi</html>")
        self.assertEqual(ticket_pages.page_path(self.settings, page.token).read_bytes(), b"<html>hi</html>")
        self.assertAlmostEqual(
            page.expires_at.timestamp(), (datetime.now(timezone.utc) + timedelta(days=30)).timestamp(), delta=5
        )
        self.settings.ticket_transcript_retention_days = 0
        self.assertIsNone((await ticket_pages.save_page(self.settings, b"x")).expires_at)

    async def test_purge_removes_only_files_nothing_points_to(self):
        directory = Path(self._dir.name)
        live, stale, fresh = "l" * 32, "s" * 32, "f" * 32
        for name in (live, stale, fresh):
            (directory / f"{name}.html").write_text("x")
        old = time.time() - 2 * 3600
        os.utime(directory / f"{live}.html", (old, old))
        os.utime(directory / f"{stale}.html", (old, old))
        pool = SimpleNamespace(execute=AsyncMock(), fetch=AsyncMock(return_value=[{"html_token": live}]))

        removed = await ticket_pages.purge(self.settings, pool=pool)

        self.assertEqual(removed, 1)
        self.assertTrue((directory / f"{live}.html").exists())
        self.assertFalse((directory / f"{stale}.html").exists())
        self.assertTrue((directory / f"{fresh}.html").exists())  # a close in progress hasn't recorded it yet
        self.assertIn("html_expires_at < now()", pool.execute.await_args.args[0])

    async def test_servable_only_while_the_row_is_live(self):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        for row, expected in (
            ({"html_expires_at": future}, True),
            ({"html_expires_at": None}, True),  # permanent
            ({"html_expires_at": past}, False),
            (None, False),
        ):
            pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row))
            self.assertIs(await ticket_pages.is_servable(TOKEN, pool=pool), expected)

    async def test_delete_page_and_record_remove_the_file(self):
        page = await ticket_pages.save_page(self.settings, b"x")
        path = ticket_pages.page_path(self.settings, page.token)
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"html_token": page.token}), execute=AsyncMock())
        self.assertTrue(await ticket_pages.delete_page(self.settings, 7, pool=pool))
        self.assertFalse(path.exists())
        self.assertIn("html_token = NULL", pool.execute.await_args.args[0])

        page = await ticket_pages.save_page(self.settings, b"x")
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"html_token": page.token}))
        self.assertTrue(await ticket_pages.delete_record(self.settings, 7, pool=pool))
        self.assertFalse(ticket_pages.page_path(self.settings, page.token).exists())

        pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        self.assertFalse(await ticket_pages.delete_page(self.settings, 8, pool=pool))
        self.assertFalse(await ticket_pages.delete_record(self.settings, 8, pool=pool))


class HostedTranscriptRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.bot = make_bot()
        self.bot.get_cog = lambda name: None
        for key, value in (
            ("ticket_transcript_dir", self._dir.name),
            ("ticket_transcript_public_url", "https://tickets.example"),
            ("ticket_transcript_retention_days", 30),
        ):
            object.__setattr__(self.bot.settings, key, value)
        (Path(self._dir.name) / f"{TOKEN}.html").write_text("<html><body>Goku: hi</body></html>", encoding="utf-8")
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.origin = {"Origin": f"http://{self.client.host}:{self.client.port}"}
        patcher = patch("bulmaai.web.routes_tickets.audit", AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def test_live_token_serves_the_page_sandboxed_without_login(self):
        with patch("bulmaai.web.routes_transcripts.is_servable", AsyncMock(return_value=True)):
            response = await self.client.get(f"/t/{TOKEN}")
        self.assertEqual(response.status, 200)
        self.assertIn("Goku: hi", await response.text())
        self.assertTrue(response.headers["Content-Type"].startswith("text/html"))
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("sandbox", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn("noindex", response.headers["X-Robots-Tag"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    async def test_expired_unknown_or_malformed_tokens_are_404(self):
        with patch("bulmaai.web.routes_transcripts.is_servable", AsyncMock(return_value=False)):
            self.assertEqual((await self.client.get(f"/t/{TOKEN}")).status, 404)
        with patch("bulmaai.web.routes_transcripts.is_servable", AsyncMock(return_value=True)):
            self.assertEqual((await self.client.get(f"/t/{'u' * 32}")).status, 404)  # live row, file gone
            self.assertEqual((await self.client.get("/t/short")).status, 404)

    async def test_tickets_hostname_serves_nothing_but_transcripts(self):
        host = {"Host": "tickets.example"}
        self.assertEqual((await self.client.get("/", headers=host)).status, 404)
        self.assertEqual((await self.client.get("/api/me", headers=host)).status, 404)
        self.assertEqual((await self.client.get("/static/panel.js", headers=host)).status, 404)
        with patch("bulmaai.web.routes_transcripts.is_servable", AsyncMock(return_value=True)):
            self.assertEqual((await self.client.get(f"/t/{TOKEN}", headers=host)).status, 200)
        self.assertEqual((await self.client.get("/", headers={})).status, 200)  # panel hostname unaffected

    async def test_detail_exposes_the_hosted_link_and_expiry(self):
        self.login(HELPER_ID)
        expires = datetime.now(timezone.utc) + timedelta(days=3)
        row = {
            "id": 7, "channel_id": 5, "channel_name": "ticket-0001", "requester_id": 42, "closed_by_id": None,
            "resolved": True, "ai_confidence": None, "message_count": 3, "title": "Crash", "tags": [],
            "knowledge_worthy": False, "closed_at": NOW, "guild_id": 1, "problem": "p", "resolution": "r",
            "transcript": "txt", "openai_file_id": None, "html_token": TOKEN, "html_expires_at": expires,
        }
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row))
        with patch("bulmaai.web.routes_tickets.get_pool", AsyncMock(return_value=pool)):
            data = await (await self.client.get("/api/transcripts/7")).json()
            self.assertEqual(data["html_url"], f"https://tickets.example/t/{TOKEN}")
            self.assertFalse(data["html_permanent"])
            self.assertEqual(data["html_expires_at"], expires.isoformat())

            row["html_expires_at"] = None
            data = await (await self.client.get("/api/transcripts/7")).json()
            self.assertTrue(data["html_permanent"])
            self.assertIsNone(data["html_expires_at"])

            row["html_expires_at"] = datetime.now(timezone.utc) - timedelta(days=1)
            data = await (await self.client.get("/api/transcripts/7")).json()
            self.assertIsNone(data["html_url"])  # expired but not purged yet
            self.assertFalse(data["html_permanent"])

            row["html_token"] = None
            self.assertIsNone((await (await self.client.get("/api/transcripts/7")).json())["html_url"])

    async def test_only_managers_keep_or_delete_transcripts(self):
        calls = {
            "keep": AsyncMock(return_value=True),
            "page": AsyncMock(return_value=True),
            "record": AsyncMock(return_value=True),
        }
        with (
            patch("bulmaai.web.routes_tickets.ticket_pages.set_permanent", calls["keep"]),
            patch("bulmaai.web.routes_tickets.ticket_pages.delete_page", calls["page"]),
            patch("bulmaai.web.routes_tickets.ticket_pages.delete_record", calls["record"]),
        ):
            self.login(HELPER_ID)
            self.assertEqual((await self.client.post("/api/transcripts/7/html", json={"permanent": True}, headers=self.origin)).status, 403)
            self.assertEqual((await self.client.delete("/api/transcripts/7/html", headers=self.origin)).status, 403)
            self.assertEqual((await self.client.delete("/api/transcripts/7", headers=self.origin)).status, 403)
            for call in calls.values():
                call.assert_not_called()

            self.login(OWNER_ID)
            self.assertEqual((await self.client.post("/api/transcripts/7/html", json={"permanent": "yes"}, headers=self.origin)).status, 400)
            self.assertEqual((await self.client.post("/api/transcripts/7/html", json={"permanent": True}, headers=self.origin)).status, 200)
            self.assertEqual(calls["keep"].await_args.args[1:], (7, True))
            self.assertEqual((await self.client.delete("/api/transcripts/7/html", headers=self.origin)).status, 200)
            self.assertEqual((await self.client.delete("/api/transcripts/7", headers=self.origin)).status, 200)
            calls["page"].return_value = False
            self.assertEqual((await self.client.delete("/api/transcripts/7/html", headers=self.origin)).status, 404)
            self.assertEqual((await self.client.delete("/api/transcripts/abc", headers=self.origin)).status, 400)
