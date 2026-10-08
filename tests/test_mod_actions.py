import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.services import mod_actions
from bulmaai.services.mod_cases import ModCase
from bulmaai.services.mod_actions import LadderStep, format_duration, parse_ladder, pick_step
from bulmaai.ui.mod_views import parse_custom_id, quick_action_buttons

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
        ids = [button.custom_id for button in quick_action_buttons(42, actions=("delete", "ban"), message=(7, 8))]
        self.assertEqual(ids, ["modqa:delete:42:7:8", "modqa:ban:42"])
        self.assertEqual(parse_custom_id(ids[0]), ("modqa", ["delete", "42", "7", "8"]))
        self.assertIsNone(parse_custom_id("rules:en"))
        self.assertIsNone(parse_custom_id(None))

    async def test_delete_needs_a_message(self):
        buttons = quick_action_buttons(42, actions=("delete", "dismiss"))
        self.assertEqual([button.custom_id for button in buttons], ["modqa:dismiss:42"])


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
        # No database here: the panel log would otherwise spend seconds retrying a connection per case.
        patcher = patch("bulmaai.services.panel_logs.record", AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        settings = SimpleNamespace(
            moderation_dm_on_action=True,
            moderation_appeals_enabled=True,
            moderation_warn_ladder="",
            moderation_log_channel_id=None,
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

    async def test_unrecorded_action_leaves_no_case_log_or_via_tag(self):
        with (
            patch("bulmaai.services.mod_cases.record_case", AsyncMock()) as record,
            patch("bulmaai.services.mod_actions.post_case_log", AsyncMock()) as post,
        ):
            result = await mod_actions.perform(
                self.bot, self.guild, action="ban", target_id=5, moderator=None, reason="phishing",
                source="console", record=False,
            )
        self.assertIsNone(result.case_id)
        record.assert_not_awaited()
        post.assert_not_awaited()
        self.assertEqual(self.guild.ban.await_args.kwargs["reason"], "phishing")

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


    async def test_escalation_case_records_the_warn_that_triggered_it(self):
        self.bot.settings.moderation_warn_ladder = "1=24h"
        record = AsyncMock(return_value=3)
        with (
            patch("bulmaai.services.mod_cases.record_case", record),
            patch("bulmaai.services.mod_cases.count_active_since", AsyncMock(return_value=1)),
            patch("bulmaai.services.mod_actions.post_case_log", AsyncMock()),
            patch.object(self.member, "timeout_for", AsyncMock(), create=True),
        ):
            await self.warn(self.staff(20))
        self.assertIsNone(record.await_args_list[0].kwargs["triggered_by"])
        escalation = record.await_args_list[1].kwargs
        self.assertEqual((escalation["action"], escalation["source"], escalation["triggered_by"]), ("timeout", "escalation", 3))
        self.assertIsNotNone(escalation["expires_at"])

    async def test_untimeout_and_unban_end_the_open_cases(self):
        self.member.remove_timeout = AsyncMock()
        with (
            patch("bulmaai.services.mod_cases.record_case", AsyncMock(return_value=4)),
            patch("bulmaai.services.mod_actions.post_case_log", AsyncMock()),
            patch("bulmaai.services.mod_actions.end_user_cases", AsyncMock(return_value=1)) as end,
        ):
            await mod_actions.perform(self.bot, self.guild, action="untimeout", target_id=5, moderator=None, reason="x")
            await mod_actions.perform(
                self.bot, self.guild, action="unban", target_id=5, moderator=None, reason="x", source="tempban"
            )
        self.assertEqual(end.await_args_list[0].args[3:], ("timeout",))
        self.assertIsNone(end.await_args_list[0].kwargs["note"])
        self.assertEqual(end.await_args_list[1].args[3:], ("ban",))
        self.assertEqual(end.await_args_list[1].kwargs["note"], "expired")


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def stored_case(**overrides):
    values = dict(
        id=12, guild_id=1, user_id=5, moderator_id=20, action="warn", reason="spam", duration_seconds=None,
        source="command", created_at=NOW,
    )
    values.update(overrides)
    return ModCase(**values)


class CardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch("bulmaai.services.panel_logs.record", AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.message = SimpleNamespace(id=777, channel=SimpleNamespace(id=66))
        self.partial = SimpleNamespace(edit=AsyncMock())
        self.channel = SimpleNamespace(
            id=66, send=AsyncMock(return_value=self.message), get_partial_message=lambda _id: self.partial
        )
        user = SimpleNamespace(
            name="spam_user", created_at=discord.utils.utcnow() - timedelta(days=12),
            display_avatar=SimpleNamespace(url="https://x/a.png"),
        )
        member = SimpleNamespace(created_at=user.created_at, joined_at=discord.utils.utcnow() - timedelta(days=3))
        settings = SimpleNamespace(
            moderation_log_channel_id=66, moderation_warn_ladder="2/7d=24h", panel_guild_id=1
        )
        self.bot = SimpleNamespace(
            settings=settings,
            get_channel=lambda _id: self.channel,
            get_user=lambda _id: user,
            get_guild=lambda _id: SimpleNamespace(get_member=lambda _uid: member),
        )

    async def test_post_case_log_sends_the_card_and_stores_where_it_went_and_the_context(self):
        with (
            patch("bulmaai.services.panel_logs.record", AsyncMock()),
            patch("bulmaai.services.mod_cases.case_number", AsyncMock(return_value=3)),
            patch("bulmaai.services.mod_cases.count_active_since", AsyncMock(return_value=1)),
            patch("bulmaai.services.mod_cases.set_card", AsyncMock()) as set_card,
        ):
            await mod_actions.post_case_log(
                self.bot, case_id=12, action="warn", user_id=5, moderator_id=20, reason="spam", triggered_by=9
            )
        self.channel.send.assert_awaited_once()
        self.assertIsInstance(self.channel.send.await_args.kwargs["view"], discord.ui.DesignerView)
        self.assertEqual(self.channel.send.await_args.kwargs["allowed_mentions"].users, False)
        kwargs = set_card.await_args.kwargs
        self.assertEqual((kwargs["channel_id"], kwargs["message_id"]), (66, 777))
        self.assertEqual(
            kwargs["context"], "📊 3rd case · `▰▱` 1/2 warns in 7d · account 12 days old · joined 3 days ago"
        )

    async def test_post_case_log_never_raises(self):
        self.channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason=""), "boom")
        with patch("bulmaai.services.panel_logs.record", AsyncMock()), patch("bulmaai.services.mod_cases.set_card", AsyncMock()), patch(
            "bulmaai.services.mod_actions._case_context", AsyncMock(return_value=None)
        ):
            await mod_actions.post_case_log(self.bot, case_id=None, action="ban", user_id=5, moderator_id=None, reason=None)

    async def test_end_case_flips_the_case_and_refreshes_its_card(self):
        ended = stored_case(active=False, ended_by=20, log_channel_id=66, log_message_id=777)
        with patch("bulmaai.services.mod_cases.deactivate_case", AsyncMock(return_value=ended)) as deactivate:
            result = await mod_actions.end_case(self.bot, 1, 12, ended_by=20, note="oops")
        self.assertIs(result, ended)
        deactivate.assert_awaited_once_with(1, 12, ended_by=20, note="oops")
        self.partial.edit.assert_awaited_once()
        self.assertIsInstance(self.partial.edit.await_args.kwargs["view"], discord.ui.DesignerView)

    async def test_end_case_on_an_inactive_case_touches_no_card(self):
        with patch("bulmaai.services.mod_cases.deactivate_case", AsyncMock(return_value=None)):
            self.assertIsNone(await mod_actions.end_case(self.bot, 1, 12, ended_by=20))
        self.partial.edit.assert_not_awaited()

    async def test_refresh_survives_discord_errors_and_cards_without_a_message(self):
        self.partial.edit.side_effect = discord.HTTPException(SimpleNamespace(status=404, reason=""), "gone")
        await mod_actions.refresh_case_card(self.bot, stored_case(log_channel_id=66, log_message_id=777))
        await mod_actions.refresh_case_card(self.bot, stored_case())
        self.assertEqual(self.partial.edit.await_count, 1)


if __name__ == "__main__":
    unittest.main()
