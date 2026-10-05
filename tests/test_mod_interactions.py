import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from discord.components import _component_factory
from discord.guild import BanEntry

from bulmaai.cogs.mod_interactions import ModInteractionsCog, TextModal
from bulmaai.services.automod_hits import AutomodHit
from bulmaai.services.mod_actions import ActionResult
from bulmaai.services.mod_cases import ModCase
from bulmaai.ui.mod_views import allowlist_view, appeal_review_view, quick_actions_view

GUILD_ID = 1
HELPER_ROLE, MOD_ROLE, ADMIN_ROLE = 10, 20, 30
PERFORM = "bulmaai.services.mod_actions.perform"
STAFF_CHANNEL = "bulmaai.services.mod_actions.staff_channel"
LIST_CASES = "bulmaai.services.mod_cases.list_cases"
RECORD_CASE = "bulmaai.services.mod_cases.record_case"
HIT_FOR_ALERT = "bulmaai.services.automod_hits.hit_for_alert"
GET_HIT = "bulmaai.services.automod_hits.get_hit"
MARK_FALSE_POSITIVE = "bulmaai.services.automod_hits.mark_false_positive"
MARK_CONFIRMED = "bulmaai.services.automod_hits.mark_confirmed"
SET_SETTING_OVERRIDE = "bulmaai.cogs.mod_interactions.set_setting_override"
JOINER_ALERT_FOR_MESSAGE = "bulmaai.services.joiner_alerts.alert_for_message"
JOINER_ALERT_SET_OUTCOME = "bulmaai.services.joiner_alerts.set_outcome"
NOT_FOUND = discord.NotFound(SimpleNamespace(status=404, reason=""), "")


class FakeResponse:
    def __init__(self):
        self.done = False
        for name in ("send_message", "send_modal", "defer", "edit_message"):
            setattr(self, name, AsyncMock(side_effect=self._respond))

    async def _respond(self, *args, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


def case(case_id, action):
    return ModCase(
        id=case_id, guild_id=GUILD_ID, user_id=5, moderator_id=None, action=action, reason="spam",
        duration_seconds=None, source="command", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def automod_hit(hit_id, *, domains=(), image_hashes=(), warn_case_id=None, timed_out=False, scam_hash_id=None):
    return AutomodHit(
        id=hit_id, guild_id=GUILD_ID, user_id=5, reason="blocked_domain", action="warn", details=None,
        domains=domains, image_hashes=image_hashes, scam_hash_id=scam_hash_id, warn_case_id=warn_case_id,
        timed_out=timed_out, alert_message_id=500, outcome=None,
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def staff_message(view):
    return SimpleNamespace(
        id=500,
        embeds=[discord.Embed(title="Moderation Alert", description="blocked   link\nin #general")],
        components=[_component_factory(payload) for payload in view.to_components()],
        edit=AsyncMock(),
    )


def disabled_ids(message):
    return {child.custom_id for child in message.edit.await_args.kwargs["view"].children if child.disabled}


def handled(message):
    fields = message.edit.await_args.kwargs["embeds"][0].fields
    return next(field.value for field in fields if field.name == "Handled")


class Base(unittest.IsolatedAsyncioTestCase):  # py-cord Views/Modals need a running loop
    def setUp(self):
        self.settings = SimpleNamespace(
            panel_guild_id=GUILD_ID,
            dev_guild_id=None,
            panel_admin_role_ids=(ADMIN_ROLE,),
            panel_moderator_role_ids=(MOD_ROLE,),
            panel_helper_role_ids=(HELPER_ROLE,),
            panel_owner_role_ids=(),
            moderation_appeals_enabled=True,
            moderation_appeals_channel_id=None,
            moderation_reports_channel_id=None,
            moderation_allowed_domains=("existing.com",),
        )
        self.guild = SimpleNamespace(id=GUILD_ID, name="DMZ", owner_id=100, fetch_ban=AsyncMock())
        self.banned = SimpleNamespace(
            id=5, mention="<@5>", name="spammer", bot=False,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc), send=AsyncMock(),
        )
        self.bot = SimpleNamespace(
            settings=self.settings,
            get_guild=lambda guild_id: self.guild if guild_id == GUILD_ID else None,
            get_user=lambda user_id: self.banned,
            fetch_user=AsyncMock(),
            reload_settings=Mock(),
        )
        self.cog = ModInteractionsCog(self.bot)
        self.channel = SimpleNamespace(id=900, send=AsyncMock())
        # Every modqa click checks whether it's resolving a raid_guard joiner alert; default to "no".
        joiner_alert_patch = patch(JOINER_ALERT_FOR_MESSAGE, AsyncMock(return_value=None))
        joiner_alert_patch.start()
        self.addCleanup(joiner_alert_patch.stop)

    def member(self, user_id, role_id=None):
        return SimpleNamespace(
            id=user_id, mention=f"<@{user_id}>", name=f"user{user_id}", bot=False, guild=self.guild,
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[SimpleNamespace(id=role_id)] if role_id else [],
        )

    def interaction(self, custom_id, user, *, guild=None, message=None):
        return SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": custom_id},
            user=user,
            guild=guild,
            message=message,
            response=FakeResponse(),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def modal_interaction(self, user, *, guild=None):
        return self.interaction("modal", user, guild=guild)


class QuickActionTests(Base):
    async def test_helper_cant_ban(self):
        helper = self.member(2, HELPER_ROLE)
        inter = self.interaction("modqa:ban:5", helper, guild=self.guild)
        with patch(PERFORM, AsyncMock()) as perform:
            await self.cog.on_interaction(inter)
        perform.assert_not_awaited()
        inter.response.send_modal.assert_not_awaited()
        self.assertIn("tier", inter.response.send_message.await_args.args[0])

    async def test_moderator_timeout_acts_and_disables_that_button(self):
        moderator = self.member(2, MOD_ROLE)
        alert = staff_message(quick_actions_view(5))
        inter = self.interaction("modqa:timeout:5", moderator, guild=self.guild, message=alert)
        result = ActionResult(action="timeout", case_id=7)
        with (
            patch(PERFORM, AsyncMock(return_value=result)) as perform,
            patch(HIT_FOR_ALERT, AsyncMock(return_value=None)),
        ):
            await self.cog.on_interaction(inter)

        kwargs = perform.await_args.kwargs
        self.assertEqual((kwargs["action"], kwargs["target_id"]), ("timeout", 5))
        self.assertEqual(kwargs["duration_seconds"], 86400)
        self.assertEqual(kwargs["source"], "alert")
        self.assertIs(kwargs["moderator"], moderator)
        self.assertEqual(kwargs["reason"], "Moderation Alert: blocked link in #general")
        self.assertEqual(disabled_ids(alert), {"modqa:timeout:5"})
        self.assertEqual(handled(alert), "Timeout 24h by <@2> | case #7")
        self.assertIn("case #7", inter.followup.send.await_args.args[0])

    async def test_dismiss_disables_everything_and_appends_to_handled(self):
        alert = staff_message(quick_actions_view(5))
        alert.embeds[0].add_field(name="Handled", value="Timeout 24h by <@3>")
        inter = self.interaction("modqa:dismiss:5", self.member(2, HELPER_ROLE), guild=self.guild, message=alert)
        with patch(PERFORM, AsyncMock()) as perform:
            await self.cog.on_interaction(inter)
        perform.assert_not_awaited()
        self.assertEqual(disabled_ids(alert), {"modqa:timeout:5", "modqa:ban:5", "modqa:dismiss:5"})
        self.assertEqual(handled(alert), "Timeout 24h by <@3>\nDismiss by <@2>")

    async def test_ban_opens_a_modal_and_acts_on_submit(self):
        admin = self.member(2, ADMIN_ROLE)
        alert = staff_message(quick_actions_view(5))
        inter = self.interaction("modqa:ban:5", admin, guild=self.guild, message=alert)
        with (
            patch(PERFORM, AsyncMock(return_value=ActionResult(action="ban", case_id=8))) as perform,
            patch(HIT_FOR_ALERT, AsyncMock(return_value=None)),
        ):
            await self.cog.on_interaction(inter)
            perform.assert_not_awaited()
            modal = inter.response.send_modal.await_args.args[0]
            self.assertIsInstance(modal, TextModal)
            self.assertEqual(modal.text.value, "Moderation Alert: blocked link in #general")

            modal.text.value = "  raiding  "
            await modal.callback(self.modal_interaction(admin, guild=self.guild))
        self.assertEqual((perform.await_args.kwargs["action"], perform.await_args.kwargs["reason"]), ("ban", "raiding"))
        self.assertEqual(disabled_ids(alert), {"modqa:ban:5"})

    async def test_delete_of_an_already_gone_message_counts_as_handled(self):
        alert = staff_message(quick_actions_view(5, actions=("delete", "ban"), message=(600, 700)))
        inter = self.interaction("modqa:delete:5:600:700", self.member(2, HELPER_ROLE), guild=self.guild, message=alert)
        partial = SimpleNamespace(delete=AsyncMock(side_effect=NOT_FOUND))
        channel = SimpleNamespace(get_partial_message=lambda message_id: partial if message_id == 700 else None)
        with patch("bulmaai.services.mod_actions.resolve_channel", AsyncMock(return_value=channel)) as resolve:
            await self.cog.on_interaction(inter)
        resolve.assert_awaited_once_with(self.bot, 600)
        partial.delete.assert_awaited_once()
        self.assertEqual(disabled_ids(alert), {"modqa:delete:5:600:700"})

    async def test_mod_action_error_is_shown(self):
        from bulmaai.services.mod_actions import ModActionError

        alert = staff_message(quick_actions_view(5))
        inter = self.interaction("modqa:timeout:5", self.member(2, MOD_ROLE), guild=self.guild, message=alert)
        with patch(PERFORM, AsyncMock(side_effect=ModActionError("That user isn't in the server.", 404))):
            await self.cog.on_interaction(inter)
        self.assertEqual(inter.followup.send.await_args.args[0], "That user isn't in the server.")
        alert.edit.assert_not_awaited()

    async def test_dismissing_a_joiner_alert_resolves_it_so_the_sweep_leaves_it_alone(self):
        moderator = self.member(2, MOD_ROLE)
        alert = staff_message(quick_actions_view(5))
        inter = self.interaction("modqa:dismiss:5", moderator, guild=self.guild, message=alert)
        record = SimpleNamespace(id=42)
        with (
            patch(JOINER_ALERT_FOR_MESSAGE, AsyncMock(return_value=record)) as lookup,
            patch(JOINER_ALERT_SET_OUTCOME, AsyncMock(return_value=True)) as set_outcome,
        ):
            await self.cog.on_interaction(inter)
        lookup.assert_awaited_once_with(alert.id)
        set_outcome.assert_awaited_once_with(42, "handled", moderator.id)

    async def test_other_custom_ids_and_types_are_ignored(self):
        admin = self.member(2, ADMIN_ROLE)
        for custom_id in ("rules:en", "modraid:end", None):
            inter = self.interaction(custom_id, admin, guild=self.guild)
            await self.cog.on_interaction(inter)
            self.assertFalse(inter.response.done, custom_id)
        command = self.interaction("modqa:ban:5", admin, guild=self.guild)
        command.type = discord.InteractionType.application_command
        await self.cog.on_interaction(command)
        self.assertFalse(command.response.done)


class AppealTests(Base):
    async def test_not_banned_any_more(self):
        self.guild.fetch_ban.side_effect = NOT_FOUND
        inter = self.interaction(f"modappeal:{GUILD_ID}", self.banned)
        await self.cog.on_interaction(inter)
        self.assertIn("no longer banned", inter.response.send_message.await_args.args[0])
        inter.response.send_modal.assert_not_awaited()

    async def test_duplicate_appeal_refused(self):
        self.guild.fetch_ban.return_value = BanEntry(reason="spam", user=self.banned)
        inter = self.interaction(f"modappeal:{GUILD_ID}", self.banned)
        with patch(LIST_CASES, AsyncMock(return_value=[case(9, "appeal"), case(8, "ban")])):
            await self.cog.on_interaction(inter)
        self.assertIn("already appealed", inter.response.send_message.await_args.args[0])
        inter.response.send_modal.assert_not_awaited()

    async def test_appeals_disabled(self):
        self.settings.moderation_appeals_enabled = False
        inter = self.interaction(f"modappeal:{GUILD_ID}", self.banned)
        await self.cog.on_interaction(inter)
        self.assertIn("closed", inter.response.send_message.await_args.args[0])

    async def test_submit_records_case_and_posts_review_buttons(self):
        self.guild.fetch_ban.return_value = BanEntry(reason="spam (via command by mod)", user=self.banned)
        inter = self.interaction(f"modappeal:{GUILD_ID}", self.banned)
        # An appeal from an earlier ban doesn't block a new one.
        cases = [case(8, "ban"), case(4, "appeal"), case(3, "ban")]
        with (
            patch(LIST_CASES, AsyncMock(return_value=cases)),
            patch(RECORD_CASE, AsyncMock(return_value=12)) as record,
            patch(STAFF_CHANNEL, AsyncMock(return_value=self.channel)),
        ):
            await self.cog.on_interaction(inter)
            modal = inter.response.send_modal.await_args.args[0]
            modal.text.value = "I'm sorry, it won't happen again."
            submit = self.modal_interaction(self.banned)
            await modal.callback(submit)

        record.assert_awaited_once_with(
            guild_id=GUILD_ID, user_id=5, moderator_id=None, action="appeal",
            reason="I'm sorry, it won't happen again.", source="appeal",
        )
        sent = self.channel.send.await_args.kwargs
        self.assertEqual(
            [child.custom_id for child in sent["view"].children],
            ["modappeal-review:accept:5", "modappeal-review:deny:5"],
        )
        fields = {field.name: field.value for field in sent["embed"].fields}
        self.assertEqual(fields["Ban reason"], "spam (via command by mod)")
        self.assertIn("#8 ban", fields["Recent cases"])
        self.assertEqual(sent["embed"].title, "Ban appeal | Case #12")
        self.assertIn("sent", submit.followup.send.await_args.args[0])

    async def test_accept_unbans_and_closes_the_review(self):
        admin = self.member(2, ADMIN_ROLE)
        review = staff_message(appeal_review_view(5))
        inter = self.interaction("modappeal-review:accept:5", admin, guild=self.guild, message=review)
        self.banned.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
        with patch(PERFORM, AsyncMock(return_value=ActionResult(action="unban", case_id=13))) as perform:
            await self.cog.on_interaction(inter)
        kwargs = perform.await_args.kwargs
        self.assertEqual(
            (kwargs["action"], kwargs["target_id"], kwargs["reason"], kwargs["source"], kwargs["moderator"]),
            ("unban", 5, "Appeal accepted", "appeal", admin),
        )
        self.banned.send.assert_awaited_once()  # tried, the failure is ignored
        self.assertEqual(disabled_ids(review), {"modappeal-review:accept:5", "modappeal-review:deny:5"})
        self.assertEqual(handled(review), "Accepted by <@2> | case #13")

    async def test_deny_needs_mod_ban_and_records_a_note(self):
        moderator = self.member(3, MOD_ROLE)
        inter = self.interaction("modappeal-review:deny:5", moderator, guild=self.guild)
        await self.cog.on_interaction(inter)
        inter.response.send_modal.assert_not_awaited()

        admin = self.member(2, ADMIN_ROLE)
        review = staff_message(appeal_review_view(5))
        inter = self.interaction("modappeal-review:deny:5", admin, guild=self.guild, message=review)
        with patch(PERFORM, AsyncMock(return_value=ActionResult(action="note", case_id=14))) as perform:
            await self.cog.on_interaction(inter)
            modal = inter.response.send_modal.await_args.args[0]
            modal.text.value = "ban evasion"
            await modal.callback(self.modal_interaction(admin, guild=self.guild))
        self.assertEqual(perform.await_args.kwargs["action"], "note")
        self.assertEqual(perform.await_args.kwargs["reason"], "Appeal denied: ban evasion")
        self.assertIn("Denied by <@2>", handled(review))


class ReportTests(Base):
    def reported(self, author):
        return SimpleNamespace(
            id=700, author=author, content="buy nitro here", attachments=[SimpleNamespace(filename="a.png")],
            channel=SimpleNamespace(id=600), jump_url="https://discord.com/channels/1/600/700",
        )

    async def report(self, ctx, message):
        await self.cog.report_message.callback(self.cog, ctx, message)  # .cog is only bound by add_cog

    def ctx(self, author):
        return SimpleNamespace(author=author, guild_id=GUILD_ID, respond=AsyncMock(), send_modal=AsyncMock())

    async def test_own_and_bot_messages_refused(self):
        reporter = self.member(3)
        ctx = self.ctx(reporter)
        await self.report(ctx, self.reported(reporter))
        self.assertIn("your own message", ctx.respond.await_args.args[0])

        bot_author = self.member(4)
        bot_author.bot = True
        await self.report(ctx, self.reported(bot_author))
        self.assertIn("bot's message", ctx.respond.await_args.args[0])
        ctx.send_modal.assert_not_awaited()

    async def test_report_posts_quick_actions_then_cooldown(self):
        reporter, author = self.member(3), self.member(5)
        ctx = self.ctx(reporter)
        await self.report(ctx, self.reported(author))
        modal = ctx.send_modal.await_args.args[0]
        modal.text.value = "scam link"
        submit = self.modal_interaction(reporter, guild=self.guild)
        with (
            patch(STAFF_CHANNEL, AsyncMock(return_value=self.channel)),
            patch(RECORD_CASE, AsyncMock(return_value=1)) as record,
        ):
            await modal.callback(submit)

        sent = self.channel.send.await_args.kwargs
        self.assertEqual(
            [child.custom_id for child in sent["view"].children],
            ["modqa:delete:5:600:700", "modqa:warn:5", "modqa:timeout:5", "modqa:ban:5", "modqa:dismiss:5"],
        )
        self.assertEqual(sent["embed"].description, "scam link")
        record.assert_awaited_once_with(
            guild_id=GUILD_ID, user_id=5, action="report", source="report", moderator_id=None,
            reason="Reported by <@3>: scam link",
        )
        self.assertEqual(submit.followup.send.await_args.args[0], "Thanks, staff will take a look.")

        await self.report(ctx, self.reported(author))
        self.assertIn("try again in", ctx.respond.await_args.args[0])
        ctx.send_modal.assert_awaited_once()

    async def test_report_still_posts_when_recording_the_case_fails(self):
        reporter, author = self.member(3), self.member(5)
        ctx = self.ctx(reporter)
        await self.report(ctx, self.reported(author))
        modal = ctx.send_modal.await_args.args[0]
        modal.text.value = "scam link"
        submit = self.modal_interaction(reporter, guild=self.guild)
        with (
            patch(STAFF_CHANNEL, AsyncMock(return_value=self.channel)),
            patch(RECORD_CASE, AsyncMock(side_effect=RuntimeError("db down"))) as record,
        ):
            await modal.callback(submit)
        record.assert_awaited_once()
        self.channel.send.assert_awaited_once()
        self.assertEqual(submit.followup.send.await_args.args[0], "Thanks, staff will take a look.")


class FalsePositiveTests(Base):
    def alert(self, actions=("timeout", "ban", "falsepos")):
        return staff_message(quick_actions_view(5, actions=actions))

    async def test_non_staff_refused(self):
        helper = self.member(2, 99)
        alert = self.alert()
        inter = self.interaction("modqa:falsepos:5", helper, guild=self.guild, message=alert)
        with patch(HIT_FOR_ALERT, AsyncMock()) as lookup:
            await self.cog.on_interaction(inter)
        lookup.assert_not_awaited()
        self.assertIn("tier", inter.response.send_message.await_args.args[0])
        alert.edit.assert_not_awaited()

    async def test_moderator_marks_false_positive_and_mentions_admin_for_domains(self):
        moderator = self.member(2, MOD_ROLE)
        alert = self.alert()
        inter = self.interaction("modqa:falsepos:5", moderator, guild=self.guild, message=alert)
        hit = automod_hit(9, domains=("scam.example",), warn_case_id=12, timed_out=True)
        undone = ["warn #12 removed", "timeout removed"]
        with (
            patch(HIT_FOR_ALERT, AsyncMock(return_value=hit)),
            patch(MARK_FALSE_POSITIVE, AsyncMock(return_value=undone)) as mark,
        ):
            await self.cog.on_interaction(inter)
        mark.assert_awaited_once_with(self.bot, self.guild, hit, moderator)
        self.assertEqual(disabled_ids(alert), {"modqa:timeout:5", "modqa:ban:5", "modqa:falsepos:5"})
        self.assertEqual(handled(alert), "False positive by <@2> (warn #12 removed, timeout removed)")
        texts = [call.args[0] for call in inter.followup.send.await_args_list]
        self.assertTrue(any("warn #12 removed, timeout removed" in text for text in texts))
        self.assertTrue(any("admin" in text.lower() for text in texts))
        for call in inter.followup.send.await_args_list:
            self.assertNotIn("view", call.kwargs)  # not an admin: no allowlist button offered

    async def test_admin_gets_allowlist_button_when_domains_present(self):
        admin = self.member(2, ADMIN_ROLE)
        alert = self.alert()
        inter = self.interaction("modqa:falsepos:5", admin, guild=self.guild, message=alert)
        hit = automod_hit(9, domains=("scam.example", "bad.example"))
        with (
            patch(HIT_FOR_ALERT, AsyncMock(return_value=hit)),
            patch(MARK_FALSE_POSITIVE, AsyncMock(return_value=["scam image #3 removed from the list"])),
        ):
            await self.cog.on_interaction(inter)
        allow_calls = [call.kwargs for call in inter.followup.send.await_args_list if "view" in call.kwargs]
        self.assertEqual(len(allow_calls), 1)
        self.assertEqual([child.custom_id for child in allow_calls[0]["view"].children], ["modtune:allow:9"])

    async def test_no_domains_sends_no_extra_followup(self):
        admin = self.member(2, ADMIN_ROLE)
        alert = self.alert()
        inter = self.interaction("modqa:falsepos:5", admin, guild=self.guild, message=alert)
        hit = automod_hit(9, domains=())
        with (
            patch(HIT_FOR_ALERT, AsyncMock(return_value=hit)),
            patch(MARK_FALSE_POSITIVE, AsyncMock(return_value=["already marked as a false positive"])),
        ):
            await self.cog.on_interaction(inter)
        self.assertEqual(inter.followup.send.await_count, 1)

    async def test_no_hit_falls_back_to_dismiss(self):
        moderator = self.member(2, MOD_ROLE)
        alert = self.alert()
        inter = self.interaction("modqa:falsepos:5", moderator, guild=self.guild, message=alert)
        with (
            patch(HIT_FOR_ALERT, AsyncMock(return_value=None)),
            patch(MARK_FALSE_POSITIVE, AsyncMock()) as mark,
        ):
            await self.cog.on_interaction(inter)
        mark.assert_not_awaited()
        self.assertEqual(disabled_ids(alert), {"modqa:timeout:5", "modqa:ban:5", "modqa:falsepos:5"})
        self.assertIn("no automod record", inter.followup.send.await_args.args[0].lower())


class LearnActionTests(Base):
    async def test_learn_deletes_and_learns_images(self):
        moderator = self.member(2, MOD_ROLE)
        alert = staff_message(quick_actions_view(5, actions=("learn", "dismiss"), message=(600, 700)))
        inter = self.interaction("modqa:learn:5:600:700", moderator, guild=self.guild, message=alert)
        partial = SimpleNamespace(delete=AsyncMock())
        channel = SimpleNamespace(get_partial_message=lambda message_id: partial if message_id == 700 else None)
        hit = automod_hit(11, image_hashes=(1, 2, 3))
        with (
            patch("bulmaai.services.mod_actions.resolve_channel", AsyncMock(return_value=channel)),
            patch(HIT_FOR_ALERT, AsyncMock(return_value=hit)),
            patch(MARK_CONFIRMED, AsyncMock(return_value=3)) as confirm,
        ):
            await self.cog.on_interaction(inter)
        partial.delete.assert_awaited_once()
        confirm.assert_awaited_once_with(hit, 2, learn=True)
        self.assertEqual(disabled_ids(alert), {"modqa:learn:5:600:700", "modqa:dismiss:5"})
        self.assertEqual(handled(alert), "Deleted & learned 3 images by <@2>")
        self.assertIn("learned 3 images", inter.followup.send.await_args.args[0])

    async def test_learn_with_no_hit_says_nothing_learned(self):
        moderator = self.member(2, MOD_ROLE)
        alert = staff_message(quick_actions_view(5, actions=("learn",), message=(600, 700)))
        inter = self.interaction("modqa:learn:5:600:700", moderator, guild=self.guild, message=alert)
        partial = SimpleNamespace(delete=AsyncMock(side_effect=NOT_FOUND))
        channel = SimpleNamespace(get_partial_message=lambda message_id: partial if message_id == 700 else None)
        with (
            patch("bulmaai.services.mod_actions.resolve_channel", AsyncMock(return_value=channel)),
            patch(HIT_FOR_ALERT, AsyncMock(return_value=None)),
            patch(MARK_CONFIRMED, AsyncMock()) as confirm,
        ):
            await self.cog.on_interaction(inter)
        confirm.assert_not_awaited()
        self.assertEqual(handled(alert), "Deleted & learned 0 images by <@2>")
        self.assertIn("nothing was learned", inter.followup.send.await_args.args[0])

    async def test_ban_click_confirms_hit_without_learning(self):
        admin = self.member(2, ADMIN_ROLE)
        alert = staff_message(quick_actions_view(5, actions=("ban",)))
        inter = self.interaction("modqa:ban:5", admin, guild=self.guild, message=alert)
        hit = automod_hit(13, image_hashes=(1,))
        with (
            patch(PERFORM, AsyncMock(return_value=ActionResult(action="ban", case_id=20))),
            patch(HIT_FOR_ALERT, AsyncMock(return_value=hit)),
            patch(MARK_CONFIRMED, AsyncMock(return_value=0)) as confirm,
        ):
            await self.cog.on_interaction(inter)
            modal = inter.response.send_modal.await_args.args[0]
            modal.text.value = "raiding"
            await modal.callback(self.modal_interaction(admin, guild=self.guild))
        confirm.assert_awaited_once_with(hit, 2, learn=False)


class TuneTests(Base):
    async def test_moderator_refused(self):
        moderator = self.member(2, MOD_ROLE)
        inter = self.interaction("modtune:allow:9", moderator, guild=self.guild)
        with patch(GET_HIT, AsyncMock()) as get_hit:
            await self.cog.on_interaction(inter)
        get_hit.assert_not_awaited()
        self.assertIn("tier", inter.response.send_message.await_args.args[0])

    async def test_admin_merges_domains_and_reloads(self):
        admin = self.member(2, ADMIN_ROLE)
        inter = self.interaction("modtune:allow:9", admin, guild=self.guild)
        hit = automod_hit(9, domains=("new.example", "existing.com"))
        with (
            patch(GET_HIT, AsyncMock(return_value=hit)),
            patch(SET_SETTING_OVERRIDE) as set_override,
        ):
            await self.cog.on_interaction(inter)
        set_override.assert_called_once_with("moderation_allowed_domains", "existing.com,new.example")
        self.bot.reload_settings.assert_called_once()
        self.assertIsNone(inter.response.edit_message.await_args.kwargs["view"])
        self.assertIn("Allowlisted", inter.response.edit_message.await_args.kwargs["content"])

    async def test_unknown_hit_or_no_domains(self):
        admin = self.member(2, ADMIN_ROLE)
        inter = self.interaction("modtune:allow:9", admin, guild=self.guild)
        with patch(GET_HIT, AsyncMock(return_value=None)):
            await self.cog.on_interaction(inter)
        self.assertIn("no domains", inter.response.send_message.await_args.args[0].lower())
        self.bot.reload_settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
