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

from discord.components import _component_factory

from bulmaai.services.cards import build_card_view, language_row
from bulmaai.services.panel_announcements import (
    AnnouncementError,
    build_allowed_mentions,
    build_message,
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
CARD = {"accent_color": "#F39C12", "blocks": [{"type": "text", "text": "Hello"}]}


def card_payload(text="Hello", **extra):
    return {"card": {"accent_color": "#F39C12", "blocks": [{"type": "text", "text": text}]}, **extra}


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
    # DesignerView needs a running event loop, hence IsolatedAsyncioTestCase.
    def setUp(self):
        self.channel = make_channel()
        self.guild = make_guild(self.channel)

    async def test_old_shape_row_fails_cleanly(self):
        with self.assertRaisesRegex(AnnouncementError, "old editor"):
            build_message(self.guild, {"content": "hello", "embeds": []})

    async def test_invalid_card_rejected(self):
        with self.assertRaises(AnnouncementError):
            build_message(self.guild, {"card": {"blocks": []}})

    async def test_valid_message_builds_view_and_mentions(self):
        built = build_message(self.guild, card_payload(mention_roles=[str(ROLE_ID)], mention_everyone=True))
        self.assertIsInstance(built.view, discord.ui.DesignerView)
        self.assertEqual(built.view.to_components()[0]["accent_color"], 0xF39C12)
        self.assertTrue(built.mentions.everyone)
        self.assertEqual([r.id for r in built.mentions.roles], [ROLE_ID])

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
        "payload": json.dumps(payload or card_payload("hello")),
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
        pool = FakePool([scheduled_row(payload=card_payload("hello world"))])
        with patch("bulmaai.services.panel_announcements.get_pool", AsyncMock(return_value=pool)):
            result = await send_scheduled(self._bot(guild), 1)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(pool.rows[1]["status"], "sent")

    async def test_send_scheduled_missing_permission_marks_failed(self):
        channel = make_channel(send_messages=False)
        guild = make_guild(channel)
        pool = FakePool([scheduled_row(payload=card_payload("hello"))])
        with patch("bulmaai.services.panel_announcements.get_pool", AsyncMock(return_value=pool)):
            result = await send_scheduled(self._bot(guild), 1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Send Messages", result["error"])
        self.assertEqual(pool.rows[1]["status"], "failed")
        self.assertEqual(len(channel.sent), 0)

    async def test_translation_failure_does_not_block_the_main_send(self):
        channel = make_channel()
        guild = make_guild(channel)
        payload = card_payload("hello", translate={"es": True})
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
        self.assertEqual((await self.write("POST", "/api/announce", card_payload("hi"))).status, 403)

    async def test_immediate_send(self):
        body = {
            "channel_id": str(CHANNEL_ID),
            "card": {"accent_color": None, "blocks": [
                {"type": "text", "text": f"Hello <@&{ROLE_ID}>"},
                {"type": "buttons", "buttons": [{"label": "Go", "url": "https://example.com", "emoji": "🧡"}]},
            ]},
            "mention_roles": [str(ROLE_ID)], "mention_everyone": False,
        }
        response = await self.write("POST", "/api/announce", body)
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.pool.rows[data["id"]]["status"], "sent")
        kwargs = self.channel.sent[0]
        self.assertEqual(set(kwargs), {"view", "allowed_mentions"})
        self.assertIsInstance(kwargs["view"], discord.ui.DesignerView)
        self.assertEqual([r.id for r in kwargs["allowed_mentions"].roles], [ROLE_ID])

    async def test_immediate_send_validation_error(self):
        body = {"channel_id": str(CHANNEL_ID), **card_payload("x" * 4001)}
        response = await self.write("POST", "/api/announce", body)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.channel.sent), 0)

    async def test_schedule_create_requires_future_time(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        body = {"channel_id": str(CHANNEL_ID), **card_payload("hi"), "send_at": past}
        response = await self.write("POST", "/api/announce", body)
        self.assertEqual(response.status, 400)

    async def test_schedule_create_edit_cancel_and_send_now(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        body = {"channel_id": str(CHANNEL_ID), **card_payload("later"), "send_at": future}
        created = await self.write("POST", "/api/announce", body)
        self.assertEqual(created.status, 200, await created.text())
        row = await created.json()
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(len(self.channel.sent), 0)

        listed = await (await self.client.get("/api/announce/scheduled")).json()
        self.assertEqual(len(listed["items"]), 1)

        new_future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        updated = await self.write("PUT", f"/api/announce/scheduled/{row['id']}", dict(body, **card_payload("edited"), send_at=new_future))
        self.assertEqual(updated.status, 200, await updated.text())

        sent_now = await self.write("POST", f"/api/announce/scheduled/{row['id']}/send-now", None)
        self.assertEqual(sent_now.status, 200, await sent_now.text())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIsInstance(self.channel.sent[0]["view"], discord.ui.DesignerView)
        self.assertIn("edited", json.loads(self.pool.rows[row["id"]]["payload"])["card"]["blocks"][0]["text"])

        # Already sent: cancel and a second send-now must both fail.
        self.assertEqual((await self.write("POST", f"/api/announce/scheduled/{row['id']}/cancel", None)).status, 409)
        self.assertEqual((await self.write("POST", f"/api/announce/scheduled/{row['id']}/send-now", None)).status, 409)

    async def test_cancel_scheduled(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        row = await (await self.write("POST", "/api/announce", {"channel_id": str(CHANNEL_ID), **card_payload("x"), "send_at": future})).json()
        cancelled = await self.write("POST", f"/api/announce/scheduled/{row['id']}/cancel", None)
        self.assertEqual(cancelled.status, 200)
        self.assertEqual(self.pool.rows[row["id"]]["status"], "cancelled")

    async def test_history_lists_sent_messages(self):
        await self.write("POST", "/api/announce", {"channel_id": str(CHANNEL_ID), **card_payload("hi")})
        history = await (await self.client.get("/api/announce/history")).json()
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(history["items"][0]["status"], "sent")

    async def test_draft_crud(self):
        created = await self.write("POST", "/api/announce/drafts", {**card_payload("draft body"), "channel_id": str(CHANNEL_ID)})
        self.assertEqual(created.status, 200, await created.text())
        row = await created.json()
        self.assertEqual(row["status"], "draft")

        listed = await (await self.client.get("/api/announce/drafts")).json()
        self.assertEqual(len(listed["items"]), 1)

        updated = await self.write("PUT", f"/api/announce/drafts/{row['id']}", {**card_payload("changed"), "channel_id": str(CHANNEL_ID)})
        self.assertEqual(updated.status, 200)
        self.assertEqual((await updated.json())["payload"]["card"]["blocks"][0]["text"], "changed")

        deleted = await self.write("DELETE", f"/api/announce/drafts/{row['id']}", None)
        self.assertEqual(deleted.status, 200)
        listed_after = await (await self.client.get("/api/announce/drafts")).json()
        self.assertEqual(listed_after["items"], [])

        self.assertEqual((await self.write("DELETE", f"/api/announce/drafts/{row['id']}", None)).status, 404)

    def _message(self, card, rows=()):
        view = build_card_view(card, extra_rows=rows)
        components = [_component_factory(c) for c in view.to_components()]
        return SimpleNamespace(
            id=555, author=SimpleNamespace(id=999), components=components,
            jump_url="https://discord.com/channels/1/10/555", edit=AsyncMock(),
        )

    async def test_load_and_edit_existing_message(self):
        card = {"accent_color": "#112233", "blocks": [
            {"type": "section", "text": "Side", "thumbnail_url": "https://x.com/a.png"},
            {"type": "separator", "divider": False, "spacing": "large"},
            {"type": "gallery", "images": [{"url": "https://x.com/b.png", "description": None}]},
        ]}
        message = self._message(card)
        self.channel.fetch_message = AsyncMock(return_value=message)

        loaded = await (await self.client.get("/api/announce/message", params={"ref": "555", "channel_id": str(CHANNEL_ID)})).json()
        self.assertEqual(loaded["card"], card)

        edited = dict(loaded["card"], blocks=[{"type": "text", "text": "New"}])
        response = await self.write("PATCH", f"/api/announce/{CHANNEL_ID}/555", {"card": edited})
        self.assertEqual(response.status, 200, await response.text())
        kwargs = message.edit.await_args.kwargs
        self.assertEqual(kwargs["view"].to_components()[0]["components"][0]["content"], "New")
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_edit_keeps_language_buttons_and_load_hides_them(self):
        message = self._message(CARD, rows=[language_row("rules")])
        self.channel.fetch_message = AsyncMock(return_value=message)
        loaded = await (await self.client.get("/api/announce/message", params={"ref": "555", "channel_id": str(CHANNEL_ID)})).json()
        self.assertEqual(loaded["card"]["blocks"], CARD["blocks"])
        await self.write("PATCH", f"/api/announce/{CHANNEL_ID}/555", {"card": loaded["card"]})
        rows = message.edit.await_args.kwargs["view"].to_components()[0]["components"]
        self.assertEqual(rows[-1]["components"][1]["custom_id"], "tpl_lang:rules:es")

    async def test_load_non_card_message_is_409(self):
        self.channel.fetch_message = AsyncMock(
            return_value=SimpleNamespace(id=5, author=SimpleNamespace(id=999), components=[], jump_url="x")
        )
        response = await self.client.get("/api/announce/message", params={"ref": "5", "channel_id": str(CHANNEL_ID)})
        self.assertEqual(response.status, 409)

    async def test_preview_and_templates_endpoints(self):
        ok = await self.write("POST", "/api/cards/preview", {"card": CARD})
        data = await ok.json()
        self.assertEqual((data["chars"], data["components"]), (5, 2))
        self.assertEqual((await self.write("POST", "/api/cards/preview", {"card": {"blocks": []}})).status, 400)
        listed = await (await self.client.get("/api/announce/templates")).json()
        self.assertEqual([t["id"] for t in listed["templates"][:2]], ["rules", "support"])


if __name__ == "__main__":
    unittest.main()
