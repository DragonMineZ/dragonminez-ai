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

from bulmaai.config import load_settings
from bulmaai.services.member_activity import MemberActivity
from bulmaai.services.mod_cases import ModCase
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app

SECRET = "test-secret"
HELPER_ROLE = 1341595261960589343
MOD_ROLE = 1472821034418962573
OWNER_ID, HELPER_ID, MOD_ID, MOD2_ID, RANDOM_ID, HIGH_ID, BOT_ID = 111, 222, 444, 445, 333, 555, 999
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeAvatar(SimpleNamespace):
    def with_size(self, _size):
        return self


class FakeColour(SimpleNamespace):
    def __str__(self):
        return f"#{self.value:06x}"


def make_member(user_id, guild, role_ids=(), position=0, joined_at=NOW):
    member = SimpleNamespace(
        id=user_id,
        name=f"user{user_id}",
        display_name=f"User {user_id}",
        global_name=None,
        bot=user_id == BOT_ID,
        guild=guild,
        roles=[SimpleNamespace(id=r, position=1, is_default=lambda: False, colour=SimpleNamespace(value=0)) for r in role_ids],
        top_role=SimpleNamespace(position=position),
        guild_permissions=SimpleNamespace(administrator=False),
        display_avatar=FakeAvatar(url="https://cdn.discordapp.com/x.png"),
        joined_at=joined_at,
        created_at=NOW,
        timed_out=False,
        communication_disabled_until=None,
        timeout_for=AsyncMock(),
        remove_timeout=AsyncMock(),
        kick=AsyncMock(),
        send=AsyncMock(),
    )
    return member


def make_bot():
    settings = load_settings(include_overrides=False)
    object.__setattr__(settings, "panel_session_secret", SECRET)
    object.__setattr__(settings, "panel_public_url", "http://127.0.0.1")
    object.__setattr__(settings, "panel_guild_id", 1)
    guild = SimpleNamespace(id=1, name="DMZ", icon=None, owner_id=OWNER_ID, channels=[], roles=[])
    members = {
        # joined_at spread out (oldest to newest) so /api/members sort order is unambiguous.
        OWNER_ID: make_member(OWNER_ID, guild, position=100, joined_at=NOW - timedelta(days=400)),
        HELPER_ID: make_member(HELPER_ID, guild, [HELPER_ROLE], position=10, joined_at=NOW - timedelta(days=300)),
        MOD_ID: make_member(MOD_ID, guild, [MOD_ROLE], position=20, joined_at=NOW - timedelta(days=200)),
        MOD2_ID: make_member(MOD2_ID, guild, [MOD_ROLE], position=20, joined_at=NOW - timedelta(days=100)),
        RANDOM_ID: make_member(RANDOM_ID, guild, position=1, joined_at=NOW - timedelta(days=50)),
        HIGH_ID: make_member(HIGH_ID, guild, position=60, joined_at=NOW - timedelta(days=10)),  # no staff tier, but role above the bot's
        BOT_ID: make_member(BOT_ID, guild, position=50, joined_at=NOW),
    }
    guild.members = list(members.values())
    guild.get_member = members.get
    guild.me = members[BOT_ID]
    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown"))
    guild.ban = AsyncMock()
    guild.unban = AsyncMock()
    bot = SimpleNamespace(
        settings=settings,
        guilds=[guild],
        get_guild=lambda _id: guild,
        user=SimpleNamespace(id=BOT_ID),
        get_user=lambda _id: None,
        fetch_user=AsyncMock(return_value=SimpleNamespace(
            id=777, name="gone", display_name="gone", bot=False, created_at=NOW,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/x.png"))),
    )
    bot.reload_settings = lambda: bot.settings
    return bot, guild, members


class ModerationPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot, self.guild, self.members = make_bot()
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.origin = f"http://{self.client.host}:{self.client.port}"
        self.record = AsyncMock(return_value=42)
        self.post_case_log = AsyncMock()
        self.warn_count = AsyncMock(return_value=0)
        for target, value in (
            ("bulmaai.web.core.get_pool", AsyncMock(return_value=SimpleNamespace(execute=AsyncMock()))),
            ("bulmaai.web.routes_moderation.mod_cases.record_case", self.record),
            ("bulmaai.services.mod_cases.count_active_since", self.warn_count),
            ("bulmaai.services.mod_cases.deactivate_user_cases_returning", AsyncMock(return_value=[])),
            ("bulmaai.services.mod_actions.post_case_log", self.post_case_log),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def post(self, path, body):
        return await self.client.post(path, json=body, headers={"Origin": self.origin})

    async def test_helper_can_warn_but_not_ban(self):
        self.login(HELPER_ID)
        response = await self.post(f"/api/users/{RANDOM_ID}/warn", {"reason": "spam"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["case_id"], 42)
        self.members[RANDOM_ID].send.assert_awaited_once()
        self.assertEqual(self.record.await_args.kwargs["action"], "warn")
        self.assertEqual(self.record.await_args.kwargs["moderator_id"], HELPER_ID)

        response = await self.post(f"/api/users/{RANDOM_ID}/ban", {"reason": "spam"})
        self.assertEqual(response.status, 403)

    async def test_reason_required(self):
        self.login(MOD_ID)
        response = await self.post(f"/api/users/{RANDOM_ID}/kick", {"reason": "  "})
        self.assertEqual(response.status, 400)
        response = await self.post(f"/api/users/{RANDOM_ID}/untimeout", {})
        self.assertEqual(response.status, 200, await response.text())

    async def test_hierarchy_rules(self):
        self.login(MOD_ID)
        cases = {
            MOD_ID: 403,  # self
            OWNER_ID: 403,  # guild owner
            BOT_ID: 403,  # the bot
            MOD2_ID: 403,  # same panel tier
            HELPER_ID: 200,  # lower tier and role
            HIGH_ID: 403,  # role above the actor's
        }
        for target, status in cases.items():
            response = await self.post(f"/api/users/{target}/note", {"reason": "x"})
            self.assertEqual(response.status, status, f"target {target}: {await response.text()}")

    async def test_bot_role_must_be_above_target(self):
        self.login(OWNER_ID)  # guild owner skips the actor role check, but Discord still needs the bot above
        response = await self.post(f"/api/users/{HIGH_ID}/kick", {"reason": "x"})
        self.assertEqual(response.status, 409)
        response = await self.post(f"/api/users/{HIGH_ID}/note", {"reason": "x"})
        self.assertEqual(response.status, 200)

    async def test_timeout_is_clamped_to_28_days(self):
        self.login(MOD_ID)
        response = await self.post(f"/api/users/{RANDOM_ID}/timeout", {"reason": "cool off", "minutes": 10**6})
        self.assertEqual(response.status, 200, await response.text())
        duration = self.members[RANDOM_ID].timeout_for.await_args.args[0]
        self.assertEqual(duration, timedelta(days=28))
        reason = self.members[RANDOM_ID].timeout_for.await_args.kwargs["reason"]
        self.assertEqual(reason, f"cool off (via panel by user{MOD_ID})")
        self.assertEqual(self.record.await_args.kwargs["duration_seconds"], 28 * 86400)

    async def test_ban_non_member_validates_hours(self):
        self.login(OWNER_ID)
        response = await self.post("/api/users/777/ban", {"reason": "raid", "delete_message_hours": 200})
        self.assertEqual(response.status, 400)
        response = await self.post("/api/users/777/ban", {"reason": "raid", "delete_message_hours": 24})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.guild.ban.await_args.kwargs["delete_message_seconds"], 24 * 3600)

    async def test_tempban_and_softban(self):
        self.login(OWNER_ID)
        response = await self.post(f"/api/users/{RANDOM_ID}/ban", {"reason": "raid", "duration": "soon"})
        self.assertEqual(response.status, 400)
        response = await self.post(f"/api/users/{RANDOM_ID}/ban", {"reason": "raid", "duration": "7d"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.record.await_args.kwargs["duration_seconds"], 7 * 86400)
        self.assertIsNotNone(self.record.await_args.kwargs["expires_at"])
        dm_view = self.members[RANDOM_ID].send.await_args.kwargs["view"]
        self.assertTrue(dm_view.children[0].custom_id.startswith("modappeal:"))

        self.guild.unban.reset_mock()
        response = await self.post(f"/api/users/{RANDOM_ID}/softban", {"reason": "spam"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.guild.ban.await_args.kwargs["delete_message_seconds"], 24 * 3600)
        self.guild.unban.assert_awaited_once()
        self.post_case_log.assert_awaited()

    async def test_warn_reaching_ladder_step_times_out(self):
        self.login(MOD_ID)
        self.warn_count.return_value = 2  # default ladder: 2 warns in 7 days -> 24h timeout
        response = await self.post(f"/api/users/{RANDOM_ID}/warn", {"reason": "spam"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["escalation"], "timeout")
        self.assertEqual(self.members[RANDOM_ID].timeout_for.await_args.args[0], timedelta(hours=24))
        self.assertEqual(self.record.await_args.kwargs["source"], "escalation")

    async def test_remove_case_only_for_warns_and_notes(self):
        self.login(MOD_ID)
        warn = ModCase(9, 1, RANDOM_ID, MOD_ID, "warn", "x", None, "panel", NOW)
        ban = ModCase(10, 1, RANDOM_ID, MOD_ID, "ban", "x", None, "panel", NOW)
        deactivate = AsyncMock(return_value=warn)
        with (
            patch("bulmaai.services.mod_cases.get_case", AsyncMock(side_effect=[warn, ban])),
            patch("bulmaai.services.mod_cases.deactivate_case", deactivate),
        ):
            first = await self.client.delete("/api/cases/9", headers={"Origin": self.origin})
            second = await self.client.delete("/api/cases/10", headers={"Origin": self.origin})
        self.assertEqual(first.status, 200, await first.text())
        self.assertEqual(second.status, 400)
        deactivate.assert_awaited_once_with(1, 9, ended_by=MOD_ID, note=None)

        self.login(HELPER_ID)
        with (
            patch("bulmaai.services.mod_cases.get_case", AsyncMock(return_value=warn)),
            patch("bulmaai.services.mod_cases.deactivate_case", AsyncMock(return_value=warn)),
        ):
            response = await self.client.delete("/api/cases/9", headers={"Origin": self.origin})
        self.assertEqual(response.status, 200, await response.text())

    async def test_helper_can_timeout_and_clear_warns_but_not_a_moderators(self):
        self.login(HELPER_ID)
        response = await self.post(f"/api/users/{RANDOM_ID}/timeout", {"reason": "spam", "minutes": 10})
        self.assertEqual(response.status, 200, await response.text())
        clear = AsyncMock(return_value=[ModCase(i, 1, RANDOM_ID, MOD_ID, "warn", "x", None, "panel", NOW) for i in (1, 2, 3)])
        with patch("bulmaai.services.mod_cases.deactivate_user_cases_returning", clear):
            response = await self.post(f"/api/users/{RANDOM_ID}/clearwarns", {})
            self.assertEqual((await response.json())["count"], 3)
            self.assertEqual((await self.post(f"/api/users/{MOD_ID}/clearwarns", {})).status, 403)
        clear.assert_awaited_once_with(1, RANDOM_ID, "warn", ended_by=HELPER_ID, note=None)

    async def test_scam_images_list_and_remove(self):
        self.login(HELPER_ID)
        item = SimpleNamespace(id=3, hash=1, source="manual", added_by=MOD_ID, note="phish", hits=2, last_hit_at=None, created_at=NOW)
        remove = AsyncMock(side_effect=[True, False])
        with (
            patch("bulmaai.services.scam_images.list_hashes", AsyncMock(return_value=[item])),
            patch("bulmaai.services.scam_images.remove", remove),
        ):
            data = await (await self.client.get("/api/scam-images")).json()
            first = await self.client.delete("/api/scam-images/3", headers={"Origin": self.origin})
            second = await self.client.delete("/api/scam-images/3", headers={"Origin": self.origin})
        self.assertEqual(data["images"][0]["added_by_id"], str(MOD_ID))
        self.assertEqual((first.status, second.status), (200, 404))
        remove.assert_awaited_with(3)

    async def test_case_edits_respect_self_and_hierarchy(self):
        self.login(MOD_ID)
        own = ModCase(9, 1, MOD_ID, HELPER_ID, "warn", "x", None, "panel", NOW)
        peer = ModCase(10, 1, MOD2_ID, OWNER_ID, "warn", "x", None, "panel", NOW)
        deactivate, update_reason = AsyncMock(), AsyncMock(return_value=True)
        with (
            patch("bulmaai.services.mod_cases.get_case", AsyncMock(side_effect=[own, peer, own])),
            patch("bulmaai.services.mod_cases.deactivate_case", deactivate),
            patch("bulmaai.services.mod_cases.update_reason", update_reason),
        ):
            for path in ("/api/cases/9", "/api/cases/10"):
                self.assertEqual((await self.client.delete(path, headers={"Origin": self.origin})).status, 403)
            response = await self.post("/api/cases/9/reason", {"reason": "never happened"})
        self.assertEqual(response.status, 403)
        deactivate.assert_not_awaited()
        update_reason.assert_not_awaited()

    async def test_search_and_profile_degrade_per_section(self):
        self.login(HELPER_ID)
        data = await (await self.client.get("/api/users/search?q=user33")).json()
        self.assertEqual([r["id"] for r in data["results"]], [str(RANDOM_ID)])

        case = ModCase(1, 1, RANDOM_ID, None, "delete", "phish", None, "automod", NOW)
        with (
            patch("bulmaai.web.routes_moderation.get_pool", AsyncMock(side_effect=OSError("db down"))),
            patch("bulmaai.web.routes_moderation.get_member_activity", AsyncMock(return_value=MemberActivity(60, 1, NOW))),
            patch("bulmaai.web.routes_moderation.list_bug_reports_by_reporter", AsyncMock(return_value=[])),
            patch("bulmaai.web.routes_moderation.mod_cases.list_cases", AsyncMock(return_value=[case])),
            self.assertLogs("bulmaai.web.routes_moderation", "ERROR"),
        ):
            response = await self.client.get(f"/api/users/{RANDOM_ID}")
        self.assertEqual(response.status, 200, await response.text())
        profile = await response.json()
        sections = profile["sections"]
        self.assertEqual(sections["activity"]["data"]["level"], 1)
        self.assertIn("error", sections["tickets"])
        self.assertIn("error", sections["dev_jar"])
        self.assertEqual(sections["cases"]["data"][0]["source"], "automod")
        self.assertNotIn("patreon", sections)  # helpers can't see Patreon

    async def test_cases_list_filters_and_paging(self):
        self.login(HELPER_ID)
        cases = [ModCase(i, 1, RANDOM_ID, HELPER_ID, "warn", "x", None, "panel", NOW) for i in (5, 4)]
        with patch("bulmaai.web.routes_moderation.mod_cases.list_cases", AsyncMock(return_value=cases)) as lister:
            data = await (await self.client.get(f"/api/cases?user_id={RANDOM_ID}&source=panel&limit=2")).json()
        lister.assert_awaited_once_with(1, user_id=RANDOM_ID, moderator_id=None, action=None, source="panel", before_id=None, limit=2)
        self.assertEqual(data["next_before_id"], 4)
        self.assertEqual(data["cases"][0]["moderator"]["id"], str(HELPER_ID))
        self.assertEqual((await self.client.get("/api/cases?limit=abc")).status, 400)

    async def test_members_list_sort_and_total(self):
        self.login(HELPER_ID)
        data = await (await self.client.get("/api/members")).json()
        self.assertEqual(data["total"], len(self.members))
        self.assertFalse(data["has_more"])
        self.assertEqual(data["members"][0]["id"], str(BOT_ID))  # joined_desc (default): most recent first
        self.assertEqual(data["members"][-1]["id"], str(OWNER_ID))

        data = await (await self.client.get("/api/members?sort=joined_asc")).json()
        self.assertEqual(data["members"][0]["id"], str(OWNER_ID))
        self.assertEqual(data["members"][-1]["id"], str(BOT_ID))

        data = await (await self.client.get("/api/members?sort=name")).json()
        names = [m["display_name"] for m in data["members"]]
        self.assertEqual(names, sorted(names, key=str.lower))

    async def test_members_filters_by_query_and_role(self):
        self.login(HELPER_ID)
        data = await (await self.client.get(f"/api/members?q=user{MOD_ID}")).json()
        self.assertEqual([m["id"] for m in data["members"]], [str(MOD_ID)])

        data = await (await self.client.get(f"/api/members?role_id={MOD_ROLE}")).json()
        self.assertEqual(sorted(m["id"] for m in data["members"]), sorted([str(MOD_ID), str(MOD2_ID)]))

    async def test_members_paging(self):
        self.login(HELPER_ID)
        first = await (await self.client.get("/api/members?limit=3&offset=0")).json()
        self.assertEqual(len(first["members"]), 3)
        self.assertTrue(first["has_more"])
        second = await (await self.client.get("/api/members?limit=3&offset=3")).json()
        self.assertEqual(len(second["members"]), 3)
        self.assertTrue(second["has_more"])
        third = await (await self.client.get("/api/members?limit=3&offset=6")).json()
        self.assertEqual(len(third["members"]), 1)
        self.assertFalse(third["has_more"])
        seen = {m["id"] for page in (first, second, third) for m in page["members"]}
        self.assertEqual(seen, {str(uid) for uid in self.members})

    async def test_members_top_role_and_tier(self):
        self.login(HELPER_ID)
        self.members[MOD_ID].roles = [
            SimpleNamespace(id=MOD_ROLE, name="Mod", position=5, is_default=lambda: False, colour=FakeColour(value=0x3498DB))
        ]
        data = await (await self.client.get(f"/api/members?q=user{MOD_ID}")).json()
        entry = data["members"][0]
        self.assertEqual(entry["tier"], "moderator")
        self.assertEqual(entry["top_role"], {"id": str(MOD_ROLE), "name": "Mod", "color": "#3498db"})
        self.assertIsNone(data["members"][0].get("timed_out_until"))

    async def test_members_bad_params(self):
        self.login(HELPER_ID)
        self.assertEqual((await self.client.get("/api/members?sort=bogus")).status, 400)
        self.assertEqual((await self.client.get("/api/members?role_id=abc")).status, 400)
        self.assertEqual((await self.client.get("/api/members?offset=-1")).status, 400)
        self.assertEqual((await self.client.get("/api/members?limit=0")).status, 400)
        self.assertEqual((await self.client.get("/api/members?limit=101")).status, 400)
        self.assertEqual((await self.client.get(f"/api/members?q={'x' * 101}")).status, 400)


if __name__ == "__main__":
    unittest.main()
