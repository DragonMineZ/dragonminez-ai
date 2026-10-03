import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.services import mod_actions
from bulmaai.services.mod_actions import LadderStep, format_duration, parse_ladder, pick_step
from bulmaai.ui.mod_views import parse_custom_id, quick_actions_view

DAY = 86400


class LadderTests(unittest.TestCase):
    def test_default_ladder_parses(self):
        self.assertEqual(
            parse_ladder("2/7d=24h, 5/30d=3d, 7/30d=ban"),
            (
                LadderStep(2, 7 * DAY, "timeout", DAY),
                LadderStep(5, 30 * DAY, "timeout", 3 * DAY),
                LadderStep(7, 30 * DAY, "ban"),
            ),
        )

    def test_bad_steps_are_skipped_and_all_time_window(self):
        with self.assertLogs("bulmaai.services.mod_actions", "WARNING"):
            steps = parse_ladder("3=kick, nonsense, 4/1d=soon")
        self.assertEqual(steps, (LadderStep(3, None, "kick"),))

    def test_timeouts_are_capped_at_28_days(self):
        self.assertEqual(parse_ladder("1=60d")[0].duration_seconds, 28 * DAY)

    def test_highest_matched_threshold_wins(self):
        steps = parse_ladder("2/7d=24h, 5/30d=3d, 7/30d=ban")
        self.assertIsNone(pick_step(steps, {7 * DAY: 1, 30 * DAY: 4}))
        self.assertEqual(pick_step(steps, {7 * DAY: 2, 30 * DAY: 4}).duration_seconds, DAY)
        self.assertEqual(pick_step(steps, {7 * DAY: 3, 30 * DAY: 5}).duration_seconds, 3 * DAY)
        self.assertEqual(pick_step(steps, {7 * DAY: 1, 30 * DAY: 9}).action, "ban")

    def test_format_duration(self):
        self.assertEqual(format_duration(None), "permanent")
        self.assertEqual(format_duration(90), "1m 30s")
        self.assertEqual(format_duration(3 * DAY + 3600 + 5), "3d 1h")


class ViewTests(unittest.IsolatedAsyncioTestCase):  # py-cord Views need a running loop
    async def test_quick_action_ids_round_trip(self):
        view = quick_actions_view(42, actions=("delete", "ban"), message=(7, 8))
        ids = [child.custom_id for child in view.children]
        self.assertEqual(ids, ["modqa:delete:42:7:8", "modqa:ban:42"])
        self.assertEqual(parse_custom_id(ids[0]), ("modqa", ["delete", "42", "7", "8"]))
        self.assertIsNone(parse_custom_id("rules:en"))
        self.assertIsNone(parse_custom_id(None))

    async def test_delete_needs_a_message(self):
        view = quick_actions_view(42, actions=("delete", "dismiss"))
        self.assertEqual([child.custom_id for child in view.children], ["modqa:dismiss:42"])


class FakeOverwrite(SimpleNamespace):
    def update(self, **values):
        self.__dict__.update(values)

    def is_empty(self):
        return self.send_messages is None and self.send_messages_in_threads is None


class LockTests(unittest.IsolatedAsyncioTestCase):
    def channel(self, send=None):
        guild = SimpleNamespace(id=1, default_role=object())
        overwrite = FakeOverwrite(send_messages=send, send_messages_in_threads=None)
        return SimpleNamespace(
            id=5, guild=guild, overwrites_for=lambda _role: overwrite, set_permissions=AsyncMock()
        )

    async def test_lock_then_unlock_restores_previous_overwrite(self):
        channel = self.channel(send=True)
        save = AsyncMock()
        with patch("bulmaai.services.mod_cases.save_lock", save):
            await mod_actions.lock_channel(channel, moderator_id=9, reason="raid")
        save.assert_awaited_once_with(1, 5, prev_send=True, prev_send_threads=None, locked_by=9)
        self.assertFalse(channel.set_permissions.await_args.kwargs["overwrite"].send_messages)

        with patch("bulmaai.services.mod_cases.pop_lock", AsyncMock(return_value=(True, None))):
            await mod_actions.unlock_channel(channel, reason="done")
        self.assertTrue(channel.set_permissions.await_args.kwargs["overwrite"].send_messages)

    async def test_unlock_without_saved_state_clears_the_overwrite(self):
        channel = self.channel(send=False)
        with patch("bulmaai.services.mod_cases.pop_lock", AsyncMock(return_value=None)):
            await mod_actions.unlock_channel(channel, reason="done")
        self.assertIsNone(channel.set_permissions.await_args.kwargs["overwrite"])


class PerformTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = SimpleNamespace(
            moderation_dm_on_action=True,
            moderation_appeals_enabled=True,
            moderation_warn_ladder="",
            moderation_log_channel_id=None,
            discord_log_channel_id=None,
            panel_guild_id=1,
            dev_guild_id=None,
            panel_owner_role_ids=(),
            panel_admin_role_ids=(),
            panel_moderator_role_ids=(20,),
            panel_helper_role_ids=(10,),
        )
        mod_actions._recent_warns.clear()
        self.member = SimpleNamespace(
            id=5, name="spammer", top_role=SimpleNamespace(position=1), send=AsyncMock(), kick=AsyncMock()
        )
        self.guild = SimpleNamespace(
            id=1,
            name="DMZ",
            owner_id=100,
            me=SimpleNamespace(top_role=SimpleNamespace(position=50)),
            get_member=lambda uid: self.member if uid == 5 else None,
            fetch_member=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason=""), "")),
            ban=AsyncMock(),
            unban=AsyncMock(),
        )
        self.bot = SimpleNamespace(settings=settings, user=SimpleNamespace(id=999))

    def staff(self, role_id):
        roles = [SimpleNamespace(id=role_id)]
        return SimpleNamespace(id=50 + role_id, name="staff", guild=self.guild, roles=roles, top_role=SimpleNamespace(position=10))

    async def warn(self, moderator):
        return await mod_actions.perform(self.bot, self.guild, action="warn", target_id=5, moderator=moderator, reason="spam")

    async def test_automatic_kick_skips_moderator_checks_but_not_bot_role(self):
        with patch("bulmaai.services.mod_cases.record_case", AsyncMock(return_value=3)):
            result = await mod_actions.perform(
                self.bot, self.guild, action="kick", target_id=5, moderator=None, reason="raid", source="antiraid"
            )
        self.assertEqual(result.case_id, 3)
        self.assertTrue(result.dm_sent)
        self.assertIn("(via antiraid by BulmaAI)", self.member.kick.await_args.kwargs["reason"])

        self.member.top_role.position = 60
        with self.assertRaises(mod_actions.ModActionError) as raised:
            await mod_actions.perform(self.bot, self.guild, action="kick", target_id=5, moderator=None, reason="x")
        self.assertEqual(raised.exception.status, 409)

    async def test_helper_warn_runs_timeouts_but_leaves_a_ban_step_to_a_moderator(self):
        self.bot.settings.moderation_warn_ladder = "1=ban"
        with (
            patch("bulmaai.services.mod_cases.record_case", AsyncMock(return_value=3)),
            patch("bulmaai.services.mod_cases.count_active_since", AsyncMock(return_value=1)),
            patch("bulmaai.services.mod_cases.update_reason", AsyncMock(return_value=True)) as update_reason,
        ):
            result = await self.warn(self.staff(10))
            self.guild.ban.assert_not_awaited()
            self.assertEqual(result.ladder_skipped, "ladder step ban skipped: needs a moderator")
            update_reason.assert_awaited_once_with(1, 3, "spam (ladder step ban skipped: needs a moderator)")

            moderator_result = await self.warn(self.staff(20))
        self.guild.ban.assert_awaited_once()
        self.assertEqual(moderator_result.escalation.action, "ban")

    async def test_same_moderator_cannot_stack_warns_on_one_user(self):
        helper = self.staff(10)
        with patch("bulmaai.services.mod_cases.record_case", AsyncMock(return_value=3)):
            await self.warn(helper)
            with self.assertRaises(mod_actions.ModActionError) as raised:
                await self.warn(helper)
            await self.warn(self.staff(20))  # another moderator still can
        self.assertEqual(raised.exception.status, 409)

    async def test_member_only_action_on_non_member(self):
        with self.assertRaises(mod_actions.ModActionError) as raised:
            await mod_actions.perform(self.bot, self.guild, action="warn", target_id=77, moderator=None, reason="x")
        self.assertEqual(raised.exception.status, 404)

    async def test_note_fails_loudly_without_db(self):
        with (
            patch("bulmaai.services.mod_cases.record_case", AsyncMock(side_effect=OSError("db down"))),
            self.assertLogs("bulmaai.services.mod_actions", "ERROR"),
            self.assertRaises(mod_actions.ModActionError) as raised,
        ):
            await mod_actions.perform(self.bot, self.guild, action="note", target_id=77, moderator=None, reason="x")
        self.assertEqual(raised.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
