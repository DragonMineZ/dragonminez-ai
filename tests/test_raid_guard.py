import asyncio
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.raid_guard import (
    NEW_ACCOUNT_TIMEOUT_SECONDS,
    RAID_TIMEOUT_SECONDS,
    RaidGuardCog,
    is_new_account,
    is_raid,
    offense_breakdown,
)
from bulmaai.services import joiner_alerts, mod_actions

JOINER_ALERTS_RECORD = "bulmaai.services.joiner_alerts.record"
JOINER_ALERTS_DUE = "bulmaai.services.joiner_alerts.due"
JOINER_ALERTS_SET_OUTCOME = "bulmaai.services.joiner_alerts.set_outcome"


def make_settings(**overrides):
    base = dict(
        panel_guild_id=1,
        dev_guild_id=None,
        moderation_enabled=True,
        moderation_raid_join_count=3,
        moderation_raid_join_window_seconds=60,
        moderation_raid_mode_minutes=10,
        moderation_raid_action="timeout",
        moderation_new_account_days=3,
        moderation_new_account_action="alert",
        moderation_log_channel_id=555,
        panel_admin_role_ids=(),
        panel_moderator_role_ids=(200,),
        panel_helper_role_ids=(100,),
        panel_owner_role_ids=(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_guild(guild_id=1, owner_id=999):
    return SimpleNamespace(id=guild_id, owner_id=owner_id, name="DMZ")


def make_member(user_id, *, guild, bot=False, created_at=None, roles=(), admin=False):
    return SimpleNamespace(
        id=user_id,
        bot=bot,
        guild=guild,
        name=f"user{user_id}",
        mention=f"<@{user_id}>",
        created_at=created_at or discord.utils.utcnow(),
        roles=list(roles),
        guild_permissions=SimpleNamespace(administrator=admin),
        top_role=SimpleNamespace(position=1),
    )


def say(member):
    return SimpleNamespace(author=member, guild=member.guild)


def make_cog(settings) -> RaidGuardCog:
    return RaidGuardCog(SimpleNamespace(settings=settings))


class PureHelperTests(unittest.TestCase):
    def test_is_raid_below_threshold(self):
        self.assertFalse(is_raid([100.0, 101.0], now=102.0, count=3, window_seconds=60))

    def test_is_raid_at_threshold_within_window(self):
        self.assertTrue(is_raid([100.0, 101.0, 102.0], now=102.0, count=3, window_seconds=60))

    def test_is_raid_ignores_joins_outside_window(self):
        # Two of these three joins are outside the 60s window, so the threshold isn't met.
        self.assertFalse(is_raid([0.0, 1.0, 102.0], now=102.0, count=3, window_seconds=60))

    def test_is_new_account_younger_than_cutoff(self):
        now = discord.utils.utcnow()
        created = now - timedelta(days=2, hours=23)
        self.assertTrue(is_new_account(created, now=now, days=3))

    def test_is_new_account_exactly_at_cutoff_is_not_new(self):
        now = discord.utils.utcnow()
        created = now - timedelta(days=3)
        self.assertFalse(is_new_account(created, now=now, days=3))

    def test_is_new_account_older_than_cutoff(self):
        now = discord.utils.utcnow()
        created = now - timedelta(days=10)
        self.assertFalse(is_new_account(created, now=now, days=3))

    def test_is_new_account_disabled_when_days_is_zero(self):
        now = discord.utils.utcnow()
        self.assertFalse(is_new_account(now, now=now, days=0))

    def test_offense_breakdown_counts_active_relevant_actions_only(self):
        cases = [
            SimpleNamespace(active=True, action="warn"),
            SimpleNamespace(active=True, action="warn"),
            SimpleNamespace(active=True, action="timeout"),
            SimpleNamespace(active=False, action="ban"),  # inactive: excluded
            SimpleNamespace(active=True, action="note"),  # not an offense action: excluded
        ]
        self.assertEqual(offense_breakdown(cases), {"warn": 2, "timeout": 1})


class RaidModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Isolate raid-mode logic from the new-account/returning-offender path (tested separately).
        self.settings = make_settings(moderation_new_account_days=0)
        self.guild = make_guild()
        self.cog = make_cog(self.settings)
        self.cog._debounce_seconds = 0  # avoid a real sleep in the debounced update task

        self.sent_messages: list[SimpleNamespace] = []

        def fake_send(**kwargs):
            message = SimpleNamespace(edit=AsyncMock(), **kwargs)
            self.sent_messages.append(message)
            return message

        self.channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))

        self.perform = AsyncMock()
        self.resolve_channel_patch = patch(
            "bulmaai.services.mod_actions.resolve_channel", AsyncMock(return_value=self.channel)
        )
        self.perform_patch = patch("bulmaai.services.mod_actions.perform", self.perform)
        self.resolve_channel_patch.start()
        self.perform_patch.start()
        self.addCleanup(self.resolve_channel_patch.stop)
        self.addCleanup(self.perform_patch.stop)

        # Outside raid mode, isolate the new-joiner path so it doesn't interfere.
        self.list_cases_patch = patch(
            "bulmaai.services.mod_cases.list_cases", AsyncMock(return_value=[])
        )
        self.list_cases_patch.start()
        self.addCleanup(self.list_cases_patch.stop)

    async def test_burst_enters_raid_mode_applies_action_and_posts_one_alert(self):
        # The first two joins don't yet meet the threshold (count=3); only the third crosses it,
        # entering raid mode and being actioned as part of that same raid.
        for i in range(3):
            member = make_member(1000 + i, guild=self.guild)
            await self.cog._handle_join(member)

        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(self.perform.await_count, 1)
        call = self.perform.await_args
        self.assertEqual(call.kwargs["action"], "timeout")
        self.assertEqual(call.kwargs["source"], "antiraid")
        self.assertEqual(call.kwargs["duration_seconds"], RAID_TIMEOUT_SECONDS)
        self.assertIsNone(call.kwargs["moderator"])

    async def test_further_joins_during_raid_debounce_into_the_same_message(self):
        for i in range(3):
            await self.cog._handle_join(make_member(2000 + i, guild=self.guild))
        for i in range(5):
            await self.cog._handle_join(make_member(3000 + i, guild=self.guild))
        await asyncio.sleep(0.05)  # let the debounced update task flush

        # Still only one alert message, but it kept being edited, not re-sent.
        self.assertEqual(self.channel.send.await_count, 1)
        message = self.sent_messages[0]
        self.assertGreaterEqual(message.edit.await_count, 1)
        # 1 for the joiner that crossed the threshold + 5 more while raid mode stayed active.
        self.assertEqual(self.perform.await_count, 6)

    async def test_raid_mode_extends_while_joins_keep_coming(self):
        for i in range(3):
            await self.cog._handle_join(make_member(4000 + i, guild=self.guild))
        first_deadline = self.cog._raid_until
        await self.cog._handle_join(make_member(4100, guild=self.guild))
        self.assertGreater(self.cog._raid_until, first_deadline)

    async def test_raid_mode_expires_and_new_joiners_fall_back_to_normal_path(self):
        for i in range(3):
            await self.cog._handle_join(make_member(5000 + i, guild=self.guild))
        self.assertEqual(self.channel.send.await_count, 1)

        # Force the raid to have ended, then join times must also be stale so a single new
        # joiner doesn't immediately re-trigger raid mode.
        import time as time_module

        self.cog._raid_until = time_module.monotonic() - 1
        self.cog._join_times.clear()

        self.perform.reset_mock()
        self.channel.send.reset_mock()
        await self.cog._handle_join(make_member(6000, guild=self.guild))

        self.perform.assert_not_awaited()  # no raid action applied
        self.channel.send.assert_not_awaited()  # no alert posted (no flags either)


class ReturningOffenderAndNewAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = make_settings(moderation_new_account_days=0)  # isolate offender-history path
        self.guild = make_guild()
        self.cog = make_cog(self.settings)

        self.channel = SimpleNamespace(send=AsyncMock())
        self.resolve_channel_patch = patch(
            "bulmaai.services.mod_actions.resolve_channel", AsyncMock(return_value=self.channel)
        )
        self.resolve_channel_patch.start()
        self.addCleanup(self.resolve_channel_patch.stop)

        # Posting an alert also records it to joiner_alerts (for the durable 1h sweep); not what these
        # tests are about, so it's mocked out here and exercised on its own below.
        self.record_alert_patch = patch(JOINER_ALERTS_RECORD, AsyncMock(return_value=1))
        self.record_alert = self.record_alert_patch.start()
        self.addCleanup(self.record_alert_patch.stop)

    async def test_no_alert_when_no_active_cases(self):
        with patch("bulmaai.services.mod_cases.list_cases", AsyncMock(return_value=[])):
            await self.cog._handle_join(make_member(1, guild=self.guild))
        self.channel.send.assert_not_awaited()

    async def test_no_alert_when_only_inactive_cases(self):
        cases = [SimpleNamespace(active=False, action="ban")]
        with patch("bulmaai.services.mod_cases.list_cases", AsyncMock(return_value=cases)):
            await self.cog._handle_join(make_member(2, guild=self.guild))
        self.channel.send.assert_not_awaited()

    async def test_alert_posted_when_active_cases_exist(self):
        cases = [SimpleNamespace(active=True, action="timeout"), SimpleNamespace(active=True, action="warn")]
        with patch("bulmaai.services.mod_cases.list_cases", AsyncMock(return_value=cases)):
            member = make_member(3, guild=self.guild)
            await self.cog._handle_join(member)
            self.channel.send.assert_not_awaited()
            await self.cog.on_message(say(member))
            await self.cog.on_message(say(member))
        self.channel.send.assert_awaited_once()
        _, kwargs = self.channel.send.await_args
        self.assertEqual([b.label for b in kwargs["view"].children], ["Kick", "Ban", "Dismiss"])
        embed = kwargs["embed"]
        history_field = next(f for f in embed.fields if f.name == "Case history")
        self.assertIn("timeout", history_field.value)
        self.assertIn("warn", history_field.value)

    async def test_alert_is_recorded_to_joiner_alerts_for_the_durable_sweep(self):
        cases = [SimpleNamespace(active=True, action="warn")]
        with patch("bulmaai.services.mod_cases.list_cases", AsyncMock(return_value=cases)):
            member = make_member(3, guild=self.guild)
            await self.cog._handle_join(member)
            await self.cog.on_message(say(member))
        self.record_alert.assert_awaited_once()
        kwargs = self.record_alert.await_args.kwargs
        self.assertEqual(kwargs["reason"], "returning_offender")
        self.assertEqual(kwargs["alert_message_id"], self.channel.send.return_value.id)

    async def test_leaving_before_speaking_drops_the_pending_alert(self):
        cases = [SimpleNamespace(active=True, action="warn")]
        with patch("bulmaai.services.mod_cases.list_cases", AsyncMock(return_value=cases)):
            member = make_member(9, guild=self.guild)
            await self.cog._handle_join(member)
            await self.cog.on_member_remove(member)
            await self.cog.on_message(say(member))
        self.channel.send.assert_not_awaited()

    async def test_db_failure_is_logged_and_join_handling_continues(self):
        with (
            patch("bulmaai.services.mod_cases.list_cases", AsyncMock(side_effect=OSError("db down"))),
            self.assertLogs("bulmaai.cogs.raid_guard", "ERROR"),
        ):
            await self.cog._handle_join(make_member(4, guild=self.guild))
        self.channel.send.assert_not_awaited()

    def make_alert(self, alert_id=1, **overrides):
        base = dict(
            id=alert_id, guild_id=1, user_id=7, reason="new_account", action_taken="alert",
            alert_message_id=111, expires_at=discord.utils.utcnow(), outcome=None,
            reviewed_by=None, reviewed_at=None, created_at=discord.utils.utcnow(),
        )
        base.update(overrides)
        return joiner_alerts.JoinerAlert(**base)

    async def test_auto_dismiss_deletes_and_logs_an_untouched_alert(self):
        cog = make_cog(self.settings)
        partial = SimpleNamespace(delete=AsyncMock())
        channel = SimpleNamespace(get_partial_message=lambda message_id: partial if message_id == 111 else None)
        with (
            patch(JOINER_ALERTS_SET_OUTCOME, AsyncMock(return_value=True)) as set_outcome,
            patch("bulmaai.services.mod_cases.record_case", AsyncMock()) as record_case,
            patch("bulmaai.services.mod_actions.resolve_channel", AsyncMock(return_value=channel)),
        ):
            await cog._auto_dismiss_joiner_alert(self.make_alert())
        set_outcome.assert_awaited_once_with(1, joiner_alerts.AUTO_DISMISSED, None)
        record_case.assert_awaited_once()
        self.assertEqual(record_case.await_args.kwargs["action"], "note")
        self.assertEqual(record_case.await_args.kwargs["source"], "antiraid")
        partial.delete.assert_awaited_once()

    async def test_auto_dismiss_skips_an_alert_a_moderator_already_claimed(self):
        # set_outcome's WHERE outcome IS NULL means a concurrent staff click wins the race.
        cog = make_cog(self.settings)
        with (
            patch(JOINER_ALERTS_SET_OUTCOME, AsyncMock(return_value=False)) as set_outcome,
            patch("bulmaai.services.mod_cases.record_case", AsyncMock()) as record_case,
            patch("bulmaai.services.mod_actions.resolve_channel", AsyncMock()) as resolve_channel,
        ):
            await cog._auto_dismiss_joiner_alert(self.make_alert())
        set_outcome.assert_awaited_once()
        record_case.assert_not_awaited()
        resolve_channel.assert_not_awaited()

    async def test_sweep_resolves_every_due_alert(self):
        cog = make_cog(self.settings)
        due = [self.make_alert(1), self.make_alert(2, user_id=8)]
        with (
            patch(JOINER_ALERTS_DUE, AsyncMock(return_value=due)),
            patch.object(cog, "_auto_dismiss_joiner_alert", AsyncMock()) as dismiss,
        ):
            await cog.expire_joiner_alerts()
        self.assertEqual(dismiss.await_count, 2)

    async def test_new_account_alert_applies_configured_action(self):
        settings = make_settings(moderation_new_account_days=3, moderation_new_account_action="timeout")
        cog = make_cog(settings)
        cog_channel = SimpleNamespace(send=AsyncMock())
        perform = AsyncMock()
        with (
            patch("bulmaai.services.mod_actions.resolve_channel", AsyncMock(return_value=cog_channel)),
            patch("bulmaai.services.mod_actions.perform", perform),
            patch("bulmaai.services.mod_cases.list_cases", AsyncMock(return_value=[])),
        ):
            new_member = make_member(
                5, guild=self.guild, created_at=discord.utils.utcnow() - timedelta(days=1)
            )
            await cog._handle_join(new_member)
            cog_channel.send.assert_not_awaited()
            await cog.on_message(say(new_member))
        perform.assert_awaited_once()
        self.assertEqual(perform.await_args.kwargs["action"], "timeout")
        self.assertEqual(perform.await_args.kwargs["source"], "antiraid")
        self.assertEqual(perform.await_args.kwargs["duration_seconds"], NEW_ACCOUNT_TIMEOUT_SECONDS)
        cog_channel.send.assert_awaited_once()


class RaidButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = make_settings()
        self.guild = make_guild()
        self.cog = make_cog(self.settings)

    def make_interaction(self, member, action):
        message = SimpleNamespace(edit=AsyncMock())
        interaction = SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": f"modraid:{action}"},
            user=member,
            guild=self.guild,
            message=message,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        return interaction

    async def test_helper_is_refused(self):
        helper = make_member(10, guild=self.guild, roles=[SimpleNamespace(id=100)])
        interaction = self.make_interaction(helper, "lockdown")
        with patch("bulmaai.services.mod_actions.lockdown", AsyncMock()) as lockdown:
            await self.cog.on_interaction(interaction)
        lockdown.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs.get("ephemeral"))

    async def test_moderator_lockdown_calls_lockdown_and_updates_view(self):
        moderator = make_member(20, guild=self.guild, roles=[SimpleNamespace(id=200)])
        interaction = self.make_interaction(moderator, "lockdown")
        with patch("bulmaai.services.mod_actions.lockdown", AsyncMock(return_value=(4, 1))) as lockdown:
            await self.cog.on_interaction(interaction)
        lockdown.assert_awaited_once()
        self.assertEqual(lockdown.await_args.args[0], self.guild)
        self.assertEqual(lockdown.await_args.kwargs["moderator_id"], moderator.id)
        interaction.response.defer.assert_awaited_once()
        interaction.message.edit.assert_awaited_once()
        edited_view = interaction.message.edit.await_args.kwargs["view"]
        ids = [child.custom_id for child in edited_view.children]
        self.assertIn("modraid:unlock", ids)
        interaction.followup.send.assert_awaited_once()
        self.assertIn("4", interaction.followup.send.await_args.args[0])

    async def test_moderator_end_clears_raid_state_and_removes_buttons(self):
        self.cog._raid_until = 999999999.0
        self.cog._join_times.extend([1.0, 2.0])
        moderator = make_member(21, guild=self.guild, roles=[SimpleNamespace(id=200)])
        interaction = self.make_interaction(moderator, "end")
        await self.cog.on_interaction(interaction)
        self.assertIsNone(self.cog._raid_until)
        self.assertEqual(len(self.cog._join_times), 0)
        interaction.response.edit_message.assert_awaited_once_with(view=None)
        interaction.followup.send.assert_awaited_once()

    async def test_non_raid_custom_ids_are_ignored(self):
        moderator = make_member(22, guild=self.guild, roles=[SimpleNamespace(id=200)])
        interaction = SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": "modqa:ban:1"},
            user=moderator,
            guild=self.guild,
            message=SimpleNamespace(edit=AsyncMock()),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await self.cog.on_interaction(interaction)
        interaction.response.send_message.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()
        interaction.response.edit_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
