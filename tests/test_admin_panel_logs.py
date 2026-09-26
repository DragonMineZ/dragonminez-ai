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

from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, RANDOM_ID, SECRET, make_member
from test_admin_panel_status import MOD_ID, make_status_bot

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
STAFF_ID = 777


def make_entry(id_, created_at, action, *, user=None, reason=None, target=None, before=None, after=None):
    """A stand-in for discord.AuditLogEntry exposing exactly the attributes routes_logs.py reads."""
    return SimpleNamespace(
        id=id_, created_at=created_at, action=action, user=user, reason=reason,
        target=target, before=before or {}, after=after or {},
    )


def make_audit_logs(entries, calls):
    """A stand-in for Guild.audit_logs: applies the same filters/limit Discord would apply server-side."""

    async def audit_logs(*, limit, before=None, user=None, action=None):
        calls.append({"limit": limit, "before": before, "user": user, "action": action})
        matched = [
            e
            for e in entries
            if (before is None or e.created_at < before)
            and (user is None or (e.user and e.user.id == user.id))
            and (action is None or e.action == action)
        ]
        for e in matched[:limit]:
            yield e

    return audit_logs


def make_logs_bot():
    bot = make_status_bot()
    guild = bot.guilds[0]
    staff = make_member(STAFF_ID, guild)
    members = {m.id: m for m in guild.members}
    members[STAFF_ID] = staff
    guild.members = list(members.values())
    guild.get_member = members.get
    guild.audit_logs = make_audit_logs([], [])
    if not hasattr(bot, "get_user"):
        bot.get_user = lambda _user_id: None
    return bot


class LogsApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_logs_bot()
        self.guild = self.bot.guilds[0]
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.pool = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(return_value=1), fetch=AsyncMock(return_value=[]))
        for target in ("bulmaai.web.core.get_pool", "bulmaai.web.routes_status.get_pool", "bulmaai.web.routes_logs.get_pool"):
            p = patch(target, AsyncMock(return_value=self.pool))
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})


class ServerLogsTests(LogsApiTestCase):
    async def test_requires_moderator(self):
        self.login(HELPER_ID)
        self.assertEqual((await self.client.get("/api/server-logs")).status, 403)
        self.assertEqual((await self.client.get("/api/server-logs/actions")).status, 403)

    async def test_actions_lists_known_audit_log_action_names(self):
        self.login(MOD_ID)
        data = await (await self.client.get("/api/server-logs/actions")).json()
        self.assertIn("ban", data["actions"])
        self.assertIn("member_role_update", data["actions"])
        self.assertEqual(data["actions"], sorted(data["actions"]))

    async def test_forbidden_is_translated_to_a_clear_403(self):
        self.login(MOD_ID)

        async def raise_forbidden(**_kwargs):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
            yield  # pragma: no cover - makes this an async generator function

        self.guild.audit_logs = raise_forbidden
        response = await self.client.get("/api/server-logs")
        self.assertEqual(response.status, 403)
        data = await response.json()
        self.assertIn("View Audit Log", data["error"])

    async def test_merges_discord_and_dyno_entries_newest_first(self):
        self.login(MOD_ID)
        staff = SimpleNamespace(
            id=STAFF_ID, name="staffer", display_name="Staffer", bot=False,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/x.png"),
        )
        kick_time = NOW - timedelta(minutes=5)
        entries = [
            make_entry(2001, NOW, discord.AuditLogAction.member_role_update, user=staff, reason="promo",
                       before={"roles": []}, after={"roles": [SimpleNamespace(name="VIP")]}),
            make_entry(2000, kick_time, discord.AuditLogAction.kick, user=staff, reason="rules"),
        ]
        calls = []
        self.guild.audit_logs = make_audit_logs(entries, calls)
        self.pool.fetch.return_value = [
            {
                "id": 5,
                "user_id": 900,
                "moderator_id": STAFF_ID,
                "action": "warn",
                "reason": "spam",
                "created_at": NOW - timedelta(minutes=2),
            }
        ]

        response = await self.client.get("/api/server-logs?limit=2")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()

        self.assertEqual([e["id"] for e in data["entries"]], ["2001", "case-5", "2000"])
        self.assertEqual(data["entries"][0]["action"], "member_role_update")
        self.assertEqual(data["entries"][0]["changes"], [{"key": "roles", "before": None, "after": "VIP"}])
        self.assertEqual(data["entries"][1]["source"], "dyno")
        self.assertEqual(data["entries"][1]["executor"]["id"], str(STAFF_ID))
        self.assertEqual(data["entries"][2]["source"], "discord")
        self.assertEqual(data["next_cursor"], kick_time.isoformat())  # not exhausted: 2 asked, 2 got

        sql, *args = self.pool.fetch.await_args.args
        self.assertIn("source = 'dyno'", sql)
        self.assertIn("action <> ALL(", sql)
        self.assertIn("created_at >= $", sql)
        self.assertIn(kick_time, args)
        self.assertNotIn("LIMIT 500", sql)

    async def test_exhausted_page_has_no_cursor_and_caps_the_dyno_backlog(self):
        self.login(MOD_ID)
        calls = []
        self.guild.audit_logs = make_audit_logs([], calls)  # Discord returns nothing -> the true end
        response = await self.client.get("/api/server-logs?limit=50")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertIsNone(data["next_cursor"])
        sql, *_args = self.pool.fetch.await_args.args
        self.assertIn("LIMIT 500", sql)
        self.assertNotIn("created_at >= $", sql)  # no lower bound once Discord's own history is exhausted

    async def test_user_filter_resolves_a_name_to_a_member_id(self):
        self.login(MOD_ID)
        calls = []
        self.guild.audit_logs = make_audit_logs([], calls)
        response = await self.client.get("/api/server-logs?user=user777")  # "user777" matches make_member's default name for STAFF_ID
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(calls[0]["user"].id, STAFF_ID)

    async def test_user_filter_with_no_match_skips_discord_entirely(self):
        self.login(MOD_ID)
        calls = []
        self.guild.audit_logs = make_audit_logs([], calls)
        response = await self.client.get("/api/server-logs?user=nobody-like-this")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(data, {"entries": [], "next_cursor": None})
        self.assertEqual(calls, [])

    async def test_unknown_action_name_is_rejected(self):
        self.login(MOD_ID)
        response = await self.client.get("/api/server-logs?action=not_a_real_action")
        self.assertEqual(response.status, 400)


class CaseFilterTests(LogsApiTestCase):
    async def test_cases_actions_lists_distinct_actions(self):
        self.pool.fetch.return_value = [{"action": "warn"}, {"action": "ban"}]
        self.login(MOD_ID)
        data = await (await self.client.get("/api/cases/actions")).json()
        self.assertEqual(data["actions"], ["ban", "warn"])

    async def test_user_id_filter_accepts_a_name(self):
        self.login(MOD_ID)
        with patch("bulmaai.web.routes_logs.mod_cases.list_cases", AsyncMock(return_value=[])) as lister:
            response = await self.client.get("/api/cases?user_id=user777")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(lister.await_args.kwargs["user_id"], STAFF_ID)

    async def test_user_id_filter_with_no_match_yields_empty_result_not_an_error(self):
        self.login(MOD_ID)
        with patch("bulmaai.web.routes_logs.mod_cases.list_cases", AsyncMock(return_value=[])) as lister:
            response = await self.client.get("/api/cases?user_id=nobody-like-this")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(lister.await_args.kwargs["user_id"], 0)

    async def test_moderator_filter_accepts_a_name(self):
        self.login(MOD_ID)
        with patch("bulmaai.web.routes_logs.mod_cases.list_cases", AsyncMock(return_value=[])) as lister:
            response = await self.client.get("/api/cases?moderator_id=user777")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(lister.await_args.kwargs["moderator_id"], STAFF_ID)
        self.assertIsNone(lister.await_args.kwargs["user_id"])


class WebsiteAuditFilterTests(LogsApiTestCase):
    async def test_audit_actions_lists_distinct_actions(self):
        self.pool.fetch.return_value = [{"action": "settings.set"}, {"action": "mod.warn"}]
        self.login(OWNER_ID)
        data = await (await self.client.get("/api/audit/actions")).json()
        self.assertEqual(data["actions"], ["settings.set", "mod.warn"])

    async def test_actor_filter_by_name_resolves_to_matching_member_ids(self):
        self.pool.fetch.return_value = []
        self.login(OWNER_ID)
        response = await self.client.get("/api/audit?actor_id=user777")
        self.assertEqual(response.status, 200, await response.text())
        sql, *args = self.pool.fetch.await_args.args
        self.assertIn("actor_id = ANY($1::bigint[])", sql)
        self.assertEqual(args[0], [STAFF_ID])

    async def test_actor_filter_by_id_is_unchanged(self):
        self.pool.fetch.return_value = []
        self.login(OWNER_ID)
        response = await self.client.get(f"/api/audit?actor_id={STAFF_ID}")
        self.assertEqual(response.status, 200, await response.text())
        sql, *args = self.pool.fetch.await_args.args
        self.assertIn("actor_id = $1", sql)
        self.assertEqual(args[0], STAFF_ID)


if __name__ == "__main__":
    unittest.main()
