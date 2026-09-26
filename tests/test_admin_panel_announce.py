import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from aiohttp.test_utils import TestClient, TestServer

from bulmaai.services.panel_announcements import (
    AnnouncementError,
    build_allowed_mentions,
    build_message,
    build_view,
    claim_scheduled,
    mark_failed,
    mark_overdue,
    mark_sent,
    send_scheduled,
)
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, SECRET, make_bot

CHANNEL_ID = 10
ROLE_ID = 55


def perms(**overrides):
    values = dict(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
    values.update(overrides)
    return SimpleNamespace(**values)


def make_channel(sent_return=None, **perm_overrides):
    sent = []

    async def send(**kwargs):
        message = sent_return() if callable(sent_return) else SimpleNamespace(id=1000 + len(sent), jump_url="https://x")
        sent.append(kwargs)
        return message

    channel = SimpleNamespace(
        id=CHANNEL_ID,
        name="announcements",
        type="news",
        permissions_for=lambda _member: perms(**perm_overrides),
        send=send,
        fetch_message=AsyncMock(),
        sent=sent,
    )
    return channel


def make_guild(channel):
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=999),
        get_channel={CHANNEL_ID: channel}.get,
        get_role={ROLE_ID: SimpleNamespace(id=ROLE_ID)}.get,
    )
    return guild


# =====================================================================================
# Unit tests: services/panel_announcements.py building blocks (no HTTP, no real Discord)
# =====================================================================================


class BuildMessageTests(unittest.IsolatedAsyncioTestCase):
    # discord.ui.View() (used when a message has buttons) requires a running event loop, hence
    # IsolatedAsyncioTestCase even though build_message() itself is a plain sync function.
    def setUp(self):
        self.channel = make_channel()
        self.guild = make_guild(self.channel)

    async def test_content_too_long_rejected(self):
        with self.assertRaises(AnnouncementError):
            build_message(self.guild, {"content": "x" * 2001})

    async def test_needs_content_or_embed(self):
        with self.assertRaises(AnnouncementError):
            build_message(self.guild, {"content": ""})

    async def test_too_many_embeds_rejected(self):
        embeds = [{"title": "t"} for _ in range(11)]
        with self.assertRaises(AnnouncementError):
            build_message(self.guild, {"embeds": embeds})

    async def test_embed_title_limit(self):
        with self.assertRaises(AnnouncementError):
            build_message(self.guild, {"embeds": [{"title": "x" * 257}]})

    async def test_too_many_fields_rejected(self):
        fields = [{"name": "n", "value": "v"} for _ in range(26)]
        with self.assertRaises(AnnouncementError):
            build_message(self.guild, {"embeds": [{"title": "t", "fields": fields}]})

    async def test_embed_total_char_limit(self):
        with self.assertRaises(AnnouncementError):
            build_message(self.guild, {"embeds": [{"description": "x" * 6001}]})

    async def test_bad_url_rejected(self):
        with self.assertRaises(AnnouncementError):
            build_message(self.guild, {"embeds": [{"title": "t", "image_url": "javascript:alert(1)"}]})

    async def test_valid_message_builds_embed_button_and_mentions(self):
        built = build_message(self.guild, {
            "content": "hi",
            "embeds": [{
                "author_name": "Author", "title": "Title", "description": "Body", "color": "#F39C12",
                "fields": [{"name": "N", "value": "V", "inline": True}],
                "footer": "foot", "timestamp": True,
            }],
            "buttons": [{"label": "Go", "url": "https://example.com"}],
            "mention_roles": [str(ROLE_ID)],
            "mention_everyone": True,
        })
        self.assertEqual(built.content, "hi")
        self.assertEqual(len(built.embeds), 1)
        raw = built.embeds[0].to_dict()
        self.assertEqual(raw["title"], "Title")
        self.assertEqual(raw["color"], 0xF39C12)
        self.assertEqual(raw["fields"][0]["name"], "N")
        self.assertIsNotNone(built.view)
        self.assertTrue(built.mentions.everyone)
        self.assertEqual([r.id for r in built.mentions.roles], [ROLE_ID])


class BuildViewAndMentionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = make_guild(make_channel())

    async def test_too_many_buttons_rejected(self):
        buttons = [{"label": "a", "url": "https://x.com"} for _ in range(6)]
        with self.assertRaises(AnnouncementError):
            build_view(buttons)

    async def test_button_without_url_rejected(self):
        with self.assertRaises(AnnouncementError):
            build_view([{"label": "a"}])

    async def test_valid_buttons_use_link_style(self):
        view = build_view([{"label": "Go", "url": "https://example.com"}])
        self.assertEqual(view.children[0].style, discord.ButtonStyle.link)

    async def test_unknown_role_rejected(self):
        with self.assertRaises(AnnouncementError):
            build_allowed_mentions(self.guild, ["9999999"], False)

    async def test_mention_everyone_must_be_bool(self):
        with self.assertRaises(AnnouncementError):
            build_allowed_mentions(self.guild, [], "yes")


# =====================================================================================
# Unit tests: claim/mark + the shared send-a-due-row path (mocked pool, fake discord objects)
# =====================================================================================


class FakePool:
    """Minimal asyncpg-pool stand-in for the scheduler's claim/mark queries."""

    def __init__(self, rows):
        self.rows = {row["id"]: dict(row) for row in rows}

    async def fetchrow(self, query, *args):
        if "SET status = 'sending'" in query:
            (row_id,) = args
            row = self.rows.get(row_id)
            if row and row["status"] == "scheduled":
                row["status"] = "sending"
                return dict(row)
            return None
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query, *args):
        if "SET status = 'sent'" in query:
            row_id, message_ids, *rest = args
            row = self.rows[row_id]
            row["status"] = "sent"
            row["message_ids"] = message_ids
            if rest:
                row["payload_patch"] = rest[0]
            return "UPDATE 1"
        if "SET status = 'failed'" in query:
            row_id, error = args
            row = self.rows.get(row_id)
            if row is None:
                return "UPDATE 0"
            if "AND status = 'scheduled'" in query and row["status"] != "scheduled":
                return "UPDATE 0"
            row["status"] = "failed"
            row["error"] = error
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {query}")


def scheduled_row(row_id=1, payload=None, send_at=None):
    return {
        "id": row_id,
        "author_id": 1,
        "channel_id": CHANNEL_ID,
        "payload": json.dumps(payload or {"content": "hello"}),
        "status": "scheduled",
        "send_at": send_at or (datetime.now(timezone.utc) - timedelta(seconds=5)),
        "error": None,
    }


class ClaimAndMarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_is_single_use(self):
        pool = FakePool([scheduled_row()])
        first = await claim_scheduled(pool, 1)
        self.assertEqual(first["id"], 1)
        second = await claim_scheduled(pool, 1)
        self.assertIsNone(second)

    async def test_mark_sent_and_failed(self):
        pool = FakePool([scheduled_row()])
        await mark_sent(pool, 1, {"main": ["123"]}, None)
        self.assertEqual(pool.rows[1]["status"], "sent")
        await mark_failed(pool, 1, "boom")
        self.assertEqual(pool.rows[1]["status"], "failed")
        self.assertEqual(pool.rows[1]["error"], "boom")

    async def test_mark_overdue_only_touches_scheduled_rows(self):
        pool = FakePool([scheduled_row(send_at=datetime.now(timezone.utc) - timedelta(hours=7))])
        ok = await mark_overdue(pool, 1, pool.rows[1]["send_at"])
        self.assertTrue(ok)
        self.assertEqual(pool.rows[1]["status"], "failed")
        self.assertIn("overdue", pool.rows[1]["error"])

        pool2 = FakePool([dict(scheduled_row(), status="cancelled")])
        self.assertFalse(await mark_overdue(pool2, 1, datetime.now(timezone.utc) - timedelta(hours=7)))


class SendScheduledTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, guild):
        return SimpleNamespace(settings=SimpleNamespace(panel_guild_id=1), get_guild=lambda _id: guild, get_cog=lambda _name: None)

    async def test_send_scheduled_success(self):
        channel = make_channel()
        guild = make_guild(channel)
        pool = FakePool([scheduled_row(payload={"content": "hello world"})])
        with patch("bulmaai.services.panel_announcements.get_pool", AsyncMock(return_value=pool)):
            result = await send_scheduled(self._bot(guild), 1)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(pool.rows[1]["status"], "sent")

    async def test_send_scheduled_missing_permission_marks_failed(self):
        channel = make_channel(send_messages=False)
        guild = make_guild(channel)
        pool = FakePool([scheduled_row(payload={"content": "hello"})])
        with patch("bulmaai.services.panel_announcements.get_pool", AsyncMock(return_value=pool)):
            result = await send_scheduled(self._bot(guild), 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Send Messages", result["error"])
        self.assertEqual(pool.rows[1]["status"], "failed")
        self.assertEqual(len(channel.sent), 0)

    async def test_translation_failure_does_not_block_the_main_send(self):
        channel = make_channel()
        guild = make_guild(channel)
        payload = {"content": "hello", "translate": {"es": True}}
        pool = FakePool([scheduled_row(payload=payload)])
        bot = self._bot(guild)
        bot.get_cog = lambda _name: object()  # truthy stand-in for the AiAnnTranslation cog
        with patch("bulmaai.services.panel_announcements.get_pool", AsyncMock(return_value=pool)), \
                patch("bulmaai.services.panel_announcements.translate_text", AsyncMock(side_effect=RuntimeError("openai down"))):
            result = await send_scheduled(bot, 1)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("es", result["translation_errors"])
        self.assertIn("openai down", result["translation_errors"]["es"])


# =====================================================================================
# HTTP-level tests: routes_announce.py through the aiohttp app
# =====================================================================================


class FakeAnnouncementsPool:
    """In-memory stand-in for the panel_announcements table, matching the literal queries
    used by routes_announce.py / panel_announcements.py."""

    def __init__(self):
        self.rows = {}
        self._next_id = 1

    def _new_row(self, **overrides):
        now = datetime.now(timezone.utc)
        row = {
            "id": None, "author_id": 1, "status": "draft", "channel_id": None, "payload": "{}",
            "send_at": None, "message_ids": None, "error": None,
            "created_at": now, "updated_at": now, "sent_at": None,
        }
        row.update(overrides)
        return row

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO panel_announcements (author_id, status, channel_id, payload, send_at)"):
            author_id, channel_id, payload, send_at = args
            row = self._new_row(id=self._next_id, author_id=author_id, status="scheduled", channel_id=channel_id, payload=payload, send_at=send_at)
            self.rows[self._next_id] = row
            self._next_id += 1
            return dict(row)
        if "VALUES ($1, 'sending'," in q:
            author_id, channel_id, payload = args
            row = self._new_row(id=self._next_id, author_id=author_id, status="sending", channel_id=channel_id, payload=payload)
            self.rows[self._next_id] = row
            self._next_id += 1
            return {"id": row["id"]}
        if "VALUES ($1, 'draft'," in q:
            author_id, channel_id, payload = args
            row = self._new_row(id=self._next_id, author_id=author_id, status="draft", channel_id=channel_id, payload=payload)
            self.rows[self._next_id] = row
            self._next_id += 1
            return dict(row)
        if "SET status = 'sending', updated_at = now()" in q:
            (row_id,) = args
            row = self.rows.get(row_id)
            if row and row["status"] == "scheduled":
                row["status"] = "sending"
                return {k: row[k] for k in ("id", "author_id", "channel_id", "payload", "send_at")}
            return None
        if q.startswith("UPDATE panel_announcements SET channel_id = $2, payload = $3::jsonb, send_at = $4"):
            row_id, channel_id, payload, send_at = args
            row = self.rows.get(row_id)
            if row and row["status"] == "scheduled":
                row.update(channel_id=channel_id, payload=payload, send_at=send_at)
                return dict(row)
            return None
        if q.startswith("UPDATE panel_announcements SET status = 'cancelled'"):
            (row_id,) = args
            row = self.rows.get(row_id)
            if row and row["status"] == "scheduled":
                row["status"] = "cancelled"
                return {"id": row_id}
            return None
        if q.startswith("UPDATE panel_announcements SET channel_id = $2, payload = $3::jsonb, updated_at = now() WHERE id = $1 AND status = 'draft'"):
            row_id, channel_id, payload = args
            row = self.rows.get(row_id)
            if row and row["status"] == "draft":
                row.update(channel_id=channel_id, payload=payload)
                return dict(row)
            return None
        raise AssertionError(f"Unhandled fetchrow: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if "status = 'scheduled' ORDER BY send_at" in q:
            return [dict(r) for r in self.rows.values() if r["status"] == "scheduled"]
        if "status NOT IN ('draft', 'scheduled')" in q:
            return [dict(r) for r in self.rows.values() if r["status"] not in ("draft", "scheduled")]
        if "status = 'draft' ORDER BY updated_at" in q:
            return [dict(r) for r in self.rows.values() if r["status"] == "draft"]
        raise AssertionError(f"Unhandled fetch: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE panel_announcements SET status = 'sent'"):
            if "payload = payload || $3::jsonb" in q:
                row_id, message_ids, patch_json = args
            else:
                row_id, message_ids = args
                patch_json = None
            row = self.rows[row_id]
            row.update(status="sent", message_ids=message_ids, sent_at=datetime.now(timezone.utc))
            if patch_json:
                merged = json.loads(row["payload"])
                merged.update(json.loads(patch_json))
                row["payload"] = json.dumps(merged)
            return "UPDATE 1"
        if q.startswith("UPDATE panel_announcements SET status = 'failed'"):
            row_id, error = args
            row = self.rows.get(row_id)
            if row is None:
                return "UPDATE 0"
            row.update(status="failed", error=error)
            return "UPDATE 1"
        if q.startswith("DELETE FROM panel_announcements WHERE id = $1 AND status = 'draft'"):
            (row_id,) = args
            row = self.rows.get(row_id)
            if row and row["status"] == "draft":
                del self.rows[row_id]
                return "DELETE 1"
            return "DELETE 0"
        if q.startswith("UPDATE panel_announcements SET payload = $2::jsonb, updated_at = now() WHERE id = $1"):
            row_id, payload = args
            row = self.rows.get(row_id)
            if row:
                row["payload"] = payload
            return "UPDATE 1"
        raise AssertionError(f"Unhandled execute: {q}")


class AnnouncePanelHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        self.bot.user = SimpleNamespace(id=999)
        self.guild = self.bot.guilds[0]
        self.guild.me = SimpleNamespace(id=999)
        self.channel = make_channel()
        voice = SimpleNamespace(id=11, name="voice", type="voice")
        self.guild.get_channel = {CHANNEL_ID: self.channel, 11: voice}.get
        self.guild.get_role = {ROLE_ID: SimpleNamespace(id=ROLE_ID)}.get

        self.pool = FakeAnnouncementsPool()
        for target in ("bulmaai.web.routes_announce.get_pool", "bulmaai.services.panel_announcements.get_pool"):
            patcher = patch(target, AsyncMock(return_value=self.pool))
            patcher.start()
            self.addCleanup(patcher.stop)
        core_pool_patch = patch("bulmaai.web.core.get_pool", AsyncMock(return_value=SimpleNamespace(execute=AsyncMock())))
        core_pool_patch.start()
        self.addCleanup(core_pool_patch.stop)
        presets_patch = patch("bulmaai.web.routes_presets.all_presets", return_value=[])
        presets_patch.start()
        self.addCleanup(presets_patch.stop)
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
        self.assertEqual((await self.write("POST", "/api/announce", {"content": "hi"})).status, 403)

    async def test_immediate_send(self):
        body = {
            "channel_id": str(CHANNEL_ID), "content": "Hello",
            "embeds": [{"title": "T", "description": "D"}],
            "buttons": [{"label": "Go", "url": "https://example.com"}],
            "mention_roles": [str(ROLE_ID)], "mention_everyone": False,
        }
        response = await self.write("POST", "/api/announce", body)
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.pool.rows[data["id"]]["status"], "sent")
        kwargs = self.channel.sent[0]
        self.assertEqual(kwargs["embeds"][0].title, "T")
        self.assertEqual([r.id for r in kwargs["allowed_mentions"].roles], [ROLE_ID])

    async def test_immediate_send_validation_error(self):
        body = {"channel_id": str(CHANNEL_ID), "embeds": [{"title": "x" * 300}]}
        response = await self.write("POST", "/api/announce", body)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.channel.sent), 0)

    async def test_schedule_create_requires_future_time(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        body = {"channel_id": str(CHANNEL_ID), "content": "hi", "send_at": past}
        response = await self.write("POST", "/api/announce", body)
        self.assertEqual(response.status, 400)

    async def test_schedule_create_edit_cancel_and_send_now(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        body = {"channel_id": str(CHANNEL_ID), "content": "later", "send_at": future}
        created = await self.write("POST", "/api/announce", body)
        self.assertEqual(created.status, 200, await created.text())
        row = await created.json()
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(len(self.channel.sent), 0)

        listed = await (await self.client.get("/api/announce/scheduled")).json()
        self.assertEqual(len(listed["items"]), 1)

        new_future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        updated = await self.write("PUT", f"/api/announce/scheduled/{row['id']}", dict(body, content="edited", send_at=new_future))
        self.assertEqual(updated.status, 200, await updated.text())

        sent_now = await self.write("POST", f"/api/announce/scheduled/{row['id']}/send-now", None)
        self.assertEqual(sent_now.status, 200, await sent_now.text())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.channel.sent[0]["content"], "edited")

        # Already sent: cancel and a second send-now must both fail.
        self.assertEqual((await self.write("POST", f"/api/announce/scheduled/{row['id']}/cancel", None)).status, 409)
        self.assertEqual((await self.write("POST", f"/api/announce/scheduled/{row['id']}/send-now", None)).status, 409)

    async def test_cancel_scheduled(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        row = await (await self.write("POST", "/api/announce", {"channel_id": str(CHANNEL_ID), "content": "x", "send_at": future})).json()
        cancelled = await self.write("POST", f"/api/announce/scheduled/{row['id']}/cancel", None)
        self.assertEqual(cancelled.status, 200)
        self.assertEqual(self.pool.rows[row["id"]]["status"], "cancelled")

    async def test_history_lists_sent_messages(self):
        await self.write("POST", "/api/announce", {"channel_id": str(CHANNEL_ID), "content": "hi"})
        history = await (await self.client.get("/api/announce/history")).json()
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(history["items"][0]["status"], "sent")

    async def test_draft_crud(self):
        created = await self.write("POST", "/api/announce/drafts", {"content": "draft body", "channel_id": str(CHANNEL_ID)})
        self.assertEqual(created.status, 200, await created.text())
        row = await created.json()
        self.assertEqual(row["status"], "draft")

        listed = await (await self.client.get("/api/announce/drafts")).json()
        self.assertEqual(len(listed["items"]), 1)

        updated = await self.write("PUT", f"/api/announce/drafts/{row['id']}", {"content": "changed", "channel_id": str(CHANNEL_ID)})
        self.assertEqual(updated.status, 200)
        self.assertEqual((await updated.json())["payload"]["content"], "changed")

        deleted = await self.write("DELETE", f"/api/announce/drafts/{row['id']}", None)
        self.assertEqual(deleted.status, 200)
        listed_after = await (await self.client.get("/api/announce/drafts")).json()
        self.assertEqual(listed_after["items"], [])

        self.assertEqual((await self.write("DELETE", f"/api/announce/drafts/{row['id']}", None)).status, 404)

    async def test_load_and_edit_existing_message_supports_fields(self):
        embed = discord.Embed(title="Old", description="Body", color=0x112233)
        embed.add_field(name="F", value="V", inline=True)
        message = SimpleNamespace(
            id=555, author=SimpleNamespace(id=999), content="old", embeds=[embed], components=[],
            jump_url="https://discord.com/channels/1/10/555", edit=AsyncMock(),
        )
        self.channel.fetch_message = AsyncMock(return_value=message)

        loaded = await (await self.client.get("/api/announce/message", params={"ref": "555", "channel_id": str(CHANNEL_ID)})).json()
        self.assertEqual(loaded["embeds"][0]["fields"][0], {"name": "F", "value": "V", "inline": True})

        body = {"content": "", "embeds": [dict(loaded["embeds"][0], title="New")]}
        response = await self.write("PATCH", f"/api/announce/{CHANNEL_ID}/555", body)
        self.assertEqual(response.status, 200, await response.text())
        kwargs = message.edit.await_args.kwargs
        self.assertEqual(kwargs["embeds"][0].title, "New")


if __name__ == "__main__":
    unittest.main()
