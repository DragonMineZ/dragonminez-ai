import os

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bulmaai.services import staff_tools
from bulmaai.services.mod_actions import ModActionError
from bulmaai.web.core import Tier

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
STAFF_ID = 100
TARGET_ID = 200000000000000001


def make_member(member_id, name="target"):
    return SimpleNamespace(id=member_id, name=name, display_name=name, roles=[], top_role=SimpleNamespace(position=1))


def make_ctx(tier=Tier.MODERATOR):
    staff = make_member(STAFF_ID, "staff")
    target = make_member(TARGET_ID)
    guild = SimpleNamespace(
        id=1,
        owner_id=999,
        members=[staff, target],
        me=SimpleNamespace(top_role=SimpleNamespace(position=50)),
        get_member=lambda uid: {STAFF_ID: staff, TARGET_ID: target}.get(uid),
        get_member_named=lambda name: target if name == target.name else None,
    )
    staff.guild = guild
    staff.top_role = SimpleNamespace(position=10)
    bot = SimpleNamespace(settings=SimpleNamespace(), user=SimpleNamespace(id=1), get_guild=lambda gid: guild)
    return staff_tools.ToolContext(
        bot=bot,
        settings=bot.settings,
        guild=guild,
        member=staff,
        tier=tier,
        origin="discord",
        channel_id=5,
        profile={"timezone": "UTC", "nickname": "S"},
        language="en",
    )


class TierGatingTests(unittest.IsolatedAsyncioTestCase):
    def names(self, tier):
        return {tool["name"] for tool in staff_tools.tools_for(tier)}

    def test_helper_cannot_see_ban_moderator_can(self):
        self.assertNotIn("ban", self.names(Tier.HELPER))
        self.assertNotIn("purge", self.names(Tier.HELPER))
        self.assertIn("ban", self.names(Tier.MODERATOR))
        self.assertIn("warn", self.names(Tier.HELPER))
        self.assertEqual(self.names(Tier.NONE), set())

    def test_schemas_are_anthropic_shaped(self):
        for tool in staff_tools.tools_for(Tier.OWNER):
            self.assertEqual(tool["input_schema"]["type"], "object")
            self.assertTrue(tool["description"])

    async def test_helper_cannot_run_ban(self):
        ctx = make_ctx(Tier.HELPER)
        with patch.object(staff_tools.staff_store, "create_action", AsyncMock()) as create:
            result = await staff_tools.run_tool("ban", {"user": str(TARGET_ID), "reason": "x"}, ctx)
        self.assertIn("error", result)
        create.assert_not_awaited()

    async def test_unknown_tool_is_an_error(self):
        self.assertIn("error", await staff_tools.run_tool("nope", {}, make_ctx()))


class ActionProposalTests(unittest.IsolatedAsyncioTestCase):
    async def test_ban_creates_pending_row_and_never_performs(self):
        ctx = make_ctx(Tier.MODERATOR)
        with (
            patch.object(staff_tools, "resolve_member", AsyncMock(return_value=ctx.guild.get_member(TARGET_ID))),
            patch("bulmaai.services.mod_actions.tier_for", side_effect=lambda m, s: Tier.MODERATOR if m.id == STAFF_ID else Tier.NONE),
            patch.object(staff_tools.staff_store, "create_action", AsyncMock(return_value=7)) as create,
            patch.object(staff_tools.mod_actions, "perform", AsyncMock()) as perform,
        ):
            result = await staff_tools.run_tool(
                "ban", {"user": str(TARGET_ID), "reason": "raid", "duration": "7d", "delete_days": 1}, ctx
            )
        self.assertEqual(result["pending_action"], 7)
        perform.assert_not_awaited()
        args = create.await_args.args
        self.assertEqual(args[2], "ban")
        self.assertEqual(args[3]["duration_seconds"], 7 * 86400)
        self.assertEqual(args[3]["user_id"], TARGET_ID)
        self.assertIn("raid", args[4])

    async def test_hierarchy_refusal_at_proposal(self):
        ctx = make_ctx(Tier.MODERATOR)
        with (
            patch.object(staff_tools, "resolve_member", AsyncMock(return_value=ctx.guild.get_member(TARGET_ID))),
            patch("bulmaai.services.mod_actions.tier_for", return_value=Tier.ADMIN),
            patch.object(staff_tools.staff_store, "create_action", AsyncMock()) as create,
        ):
            result = await staff_tools.run_tool("warn", {"user": str(TARGET_ID), "reason": "x"}, ctx)
        self.assertIn("error", result)
        create.assert_not_awaited()

    async def test_timeout_needs_a_readable_duration(self):
        ctx = make_ctx(Tier.HELPER)
        with (
            patch.object(staff_tools, "resolve_member", AsyncMock(return_value=ctx.guild.get_member(TARGET_ID))),
            patch("bulmaai.services.mod_actions.tier_for", side_effect=lambda m, s: Tier.MODERATOR if m.id == STAFF_ID else Tier.NONE),
            patch.object(staff_tools.staff_store, "create_action", AsyncMock(return_value=1)) as create,
        ):
            bad = await staff_tools.run_tool("timeout", {"user": str(TARGET_ID), "reason": "x", "duration": "soon"}, ctx)
            ok = await staff_tools.run_tool("timeout", {"user": str(TARGET_ID), "reason": "x", "duration": "2h"}, ctx)
        self.assertIn("error", bad)
        self.assertEqual(ok["pending_action"], 1)
        self.assertEqual(create.await_count, 1)

    def test_action_tools_are_the_proposing_ones(self):
        self.assertTrue({"warn", "ban", "purge", "ticket_reply", "clearwarns"} <= staff_tools.ACTION_TOOLS)
        self.assertNotIn("member_lookup", staff_tools.ACTION_TOOLS)
        self.assertNotIn("add_reminder", staff_tools.ACTION_TOOLS)


class ExecuteActionTests(unittest.IsolatedAsyncioTestCase):
    def row(self, action="warn"):
        return {"id": 1, "action": action, "guild_id": 1, "args": {"user_id": TARGET_ID, "reason": "spam"}}

    async def test_execute_passes_bulma_ai_source(self):
        ctx = make_ctx(Tier.MODERATOR)
        perform = AsyncMock(return_value=SimpleNamespace(case_id=5, ladder_skipped=None, escalation=None))
        with (
            patch.object(staff_tools, "tier_for", return_value=Tier.MODERATOR),
            patch.object(staff_tools.mod_actions, "perform", perform),
        ):
            text = await staff_tools.execute_action(self.row(), ctx.bot, ctx.member)
        kwargs = perform.await_args.kwargs
        self.assertEqual(kwargs["source"], "Bulma_AI")
        self.assertIs(kwargs["moderator"], ctx.member)
        self.assertEqual(kwargs["action"], "warn")
        self.assertIn("Case #5", text)

    async def test_execute_rechecks_tier(self):
        ctx = make_ctx(Tier.MODERATOR)
        perform = AsyncMock()
        with (
            patch.object(staff_tools, "tier_for", return_value=Tier.HELPER),
            patch.object(staff_tools.mod_actions, "perform", perform),
        ):
            with self.assertRaises(ValueError):
                await staff_tools.execute_action(self.row("ban"), ctx.bot, ctx.member)
        perform.assert_not_awaited()

    async def test_execute_propagates_mod_action_error(self):
        ctx = make_ctx(Tier.MODERATOR)
        with (
            patch.object(staff_tools, "tier_for", return_value=Tier.MODERATOR),
            patch.object(staff_tools.mod_actions, "perform", AsyncMock(side_effect=ModActionError("tier", 403))),
        ):
            with self.assertRaises(ModActionError):
                await staff_tools.execute_action(self.row(), ctx.bot, ctx.member)


class ParseWhenTests(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(staff_tools.parse_when("2h", "UTC", now=NOW), NOW + timedelta(hours=2))
        self.assertEqual(staff_tools.parse_when("30m", "UTC", now=NOW), NOW + timedelta(minutes=30))
        self.assertEqual(staff_tools.parse_when("in 1h30m", "UTC", now=NOW), NOW + timedelta(minutes=90))
        self.assertEqual(staff_tools.parse_when("3 days", "UTC", now=NOW), NOW + timedelta(days=3))

    def test_tomorrow_in_profile_timezone(self):
        try:
            zone = ZoneInfo("America/Mexico_City")
        except ZoneInfoNotFoundError:
            self.skipTest("no tz database")
        due = staff_tools.parse_when("tomorrow 9:00", "America/Mexico_City", now=NOW)
        self.assertEqual((due.astimezone(zone).day, due.astimezone(zone).hour), (13, 9))

    def test_tomorrow_utc_and_iso(self):
        self.assertEqual(
            staff_tools.parse_when("tomorrow 9:00", "UTC", now=NOW), datetime(2026, 11, 13, 9, tzinfo=timezone.utc)
        )
        self.assertEqual(
            staff_tools.parse_when("2026-11-20T10:30", "UTC", now=NOW), datetime(2026, 11, 20, 10, 30, tzinfo=timezone.utc)
        )

    def test_rejections(self):
        for bad in ("", "soon", "yesterday 9:00", "2026-11-01T10:00", "91d", "today 25:00", "2h junk", "today 9:00"):
            with self.assertRaises(ValueError, msg=bad):
                staff_tools.parse_when(bad, "UTC", now=NOW)


class OwnDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_my_messages_only_queries_callers_id(self):
        ctx = make_ctx(Tier.HELPER)
        search = AsyncMock(return_value=[{"content": "hi", "channel_id": 3, "created_at": NOW}])
        with patch.object(staff_tools.staff_store, "search_vault", search):
            result = await staff_tools.run_tool("search_my_messages", {"query": "hi", "user": "999"}, ctx)
        self.assertEqual(search.await_args.args[0], STAFF_ID)
        self.assertEqual(result["messages"][0]["text"], "hi")

    async def test_add_reminder_stores_for_caller_and_rejects_past(self):
        ctx = make_ctx(Tier.HELPER)
        add = AsyncMock(return_value=3)
        with patch.object(staff_tools.staff_store, "add_reminder", add):
            ok = await staff_tools.run_tool("add_reminder", {"text": "check appeal", "when": "2h"}, ctx)
            bad = await staff_tools.run_tool("add_reminder", {"text": "x", "when": "2001-01-01T00:00"}, ctx)
        self.assertEqual(ok["reminder"], 3)
        self.assertEqual(add.await_args.args[0], STAFF_ID)
        self.assertIn("error", bad)
        self.assertEqual(add.await_count, 1)

    async def test_forget_note_is_scoped_to_caller(self):
        ctx = make_ctx(Tier.HELPER)
        delete = AsyncMock(return_value=True)
        with patch.object(staff_tools.staff_store, "delete_note", delete):
            await staff_tools.run_tool("forget_note", {"note_id": 4}, ctx)
        delete.assert_awaited_once_with(STAFF_ID, 4)


if __name__ == "__main__":
    unittest.main()
