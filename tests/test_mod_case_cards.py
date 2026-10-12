import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import mod_case_cards
from bulmaai.cogs.mod_case_cards import ModCaseCardsCog
from bulmaai.services.mod_actions import ModActionError
from bulmaai.services.mod_cases import ModCase
from bulmaai.ui import v2
from bulmaai.ui.v2 import card
from bulmaai.web.core import Tier
from v2_helpers import sent_message

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CASES = "bulmaai.services.mod_cases."
ACTIONS = "bulmaai.services.mod_actions."
LOG_MESSAGE = 900  # the case's mod-log card, the message the tests click


def case(**overrides):
    values = dict(
        id=12, guild_id=1, user_id=5, moderator_id=20, action="warn", reason="spam", duration_seconds=None,
        source="command", created_at=NOW, log_channel_id=800, log_message_id=LOG_MESSAGE,
    )
    values.update(overrides)
    return ModCase(**values)


def click(custom_id, guild_id=1):
    done = []

    async def defer():
        done.append(True)

    async def edit_message(**_):
        done.append(True)

    return SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": custom_id},
        guild=SimpleNamespace(id=guild_id),
        user=SimpleNamespace(id=50),
        message=sent_message(card("case", buttons=[discord.ui.Button(label="Undo", custom_id=custom_id)]), message_id=LOG_MESSAGE),
        response=SimpleNamespace(
            is_done=lambda: bool(done),
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
            defer=AsyncMock(side_effect=defer),
            edit_message=AsyncMock(side_effect=edit_message),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class CaseCardButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(settings=SimpleNamespace(panel_guild_id=1))
        self.cog = ModCaseCardsCog(self.bot)
        self.tier = Tier.MODERATOR
        patcher = patch.object(mod_case_cards, "tier_for", lambda *_: self.tier)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in (patch.object(mod_case_cards, "resolve_member", AsyncMock(return_value=None)),):
            target.start()
            self.addCleanup(target.stop)

    def sent(self, interaction):
        return interaction.response.send_message.await_args.args[0]

    async def test_other_components_are_ignored(self):
        interaction = click("modqa:ban:5")
        await self.cog.on_interaction(interaction)
        interaction.response.send_message.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()

    async def test_a_helper_cannot_unban(self):
        self.tier = Tier.HELPER
        interaction = click("modcase:undo:12")
        with (
            patch(CASES + "get_case", AsyncMock(return_value=case(action="ban"))),
            patch(ACTIONS + "perform", AsyncMock()) as perform,
        ):
            await self.cog.on_interaction(interaction)
        self.assertEqual(self.sent(interaction), "Your staff tier can't do that.")
        perform.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_other_guilds_are_refused(self):
        interaction = click("modcase:history:12", guild_id=2)
        with patch(CASES + "get_case", AsyncMock(return_value=case())):
            await self.cog.on_interaction(interaction)
        self.assertIn("only works", self.sent(interaction))

    async def test_undoing_a_warn_ends_the_case_as_the_clicker(self):
        interaction = click("modcase:undo:12")
        with (
            patch(CASES + "get_case", AsyncMock(return_value=case())),
            patch(ACTIONS + "check_hierarchy"),
            patch(ACTIONS + "end_case", AsyncMock(return_value=case(active=False))) as end,
        ):
            await self.cog.on_interaction(interaction)
        end.assert_awaited_once_with(self.bot, 1, 12, ended_by=50)
        busy = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertIn(v2.WORKING, [item.label for item in v2._action_rows(busy.children) for item in item.children])
        interaction.response.send_message.assert_not_awaited()  # the card changing is the confirmation
        interaction.followup.send.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()  # end_case re-rendered the mod-log card

    async def test_undoing_a_timeout_lifts_it_and_reports_errors_privately(self):
        interaction = click("modcase:undo:12")
        timeout = case(action="timeout", duration_seconds=3600)
        with (
            patch(CASES + "get_case", AsyncMock(return_value=timeout)),
            patch(ACTIONS + "check_hierarchy"),
            patch(ACTIONS + "perform", AsyncMock(side_effect=ModActionError("That user isn't in the server.", 404))) as perform,
        ):
            await self.cog.on_interaction(interaction)
        kwargs = perform.await_args.kwargs
        self.assertEqual((kwargs["action"], kwargs["reason"], kwargs["source"]), ("untimeout", "Undone from case #12", "case card"))
        self.assertEqual(interaction.followup.send.await_args.args[0], "That user isn't in the server.")
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        restored = interaction.edit_original_response.await_args.kwargs["view"]  # the card comes back untouched
        self.assertNotIn(v2.WORKING, [item.label for item in v2._action_rows(restored.children) for item in item.children])

    async def test_hierarchy_blocks_undo(self):
        interaction = click("modcase:undo:12")
        with (
            patch(CASES + "get_case", AsyncMock(return_value=case())),
            patch(ACTIONS + "check_hierarchy", side_effect=ModActionError("That user's top role is equal to or above yours.", 403)),
            patch(ACTIONS + "end_case", AsyncMock()) as end,
        ):
            await self.cog.on_interaction(interaction)
        end.assert_not_awaited()
        self.assertIn("top role", self.sent(interaction))

    async def test_edit_opens_a_prefilled_modal_that_updates_and_refreshes(self):
        interaction = click("modcase:edit:12")
        with patch(CASES + "get_case", AsyncMock(return_value=case())), patch(ACTIONS + "check_hierarchy"):
            await self.cog.on_interaction(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual(modal.reason.value, "spam")
        self.assertEqual(modal.reason.max_length, 400)

        submit = click("modcase-edit:12")
        edited = case(reason="new")
        with (
            patch(CASES + "update_reason", AsyncMock(return_value=True)) as update,
            patch(CASES + "get_case", AsyncMock(return_value=edited)),
            patch(ACTIONS + "refresh_case_card", AsyncMock()) as refresh,
        ):
            await modal.on_submit(submit, "new")
        update.assert_awaited_once_with(1, 12, "new")
        refresh.assert_awaited_once_with(self.bot, edited)
        submit.response.defer.assert_awaited_once()

    async def test_history_lists_the_users_recent_cases_privately(self):
        interaction = click("modcase:history:12")
        cases = [case(id=13, action="timeout"), case(id=12, active=False)]
        with (
            patch(CASES + "get_case", AsyncMock(return_value=case())),
            patch(CASES + "list_cases", AsyncMock(return_value=cases)) as listing,
        ):
            await self.cog.on_interaction(interaction)
        self.assertEqual(listing.await_args.kwargs, {"user_id": 5, "limit": 15})
        text = self.sent(interaction)
        self.assertIn("`#13` **timeout**", text)
        self.assertIn("~~`#12` **warn**", text)
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_a_crash_replies_something_went_wrong(self):
        interaction = click("modcase:history:12")
        with (
            patch(CASES + "get_case", AsyncMock(side_effect=OSError("db down"))),
            self.assertLogs("bulmaai.cogs.mod_case_cards", "ERROR"),
        ):
            await self.cog.on_interaction(interaction)
        self.assertIn("Something went wrong", self.sent(interaction))


if __name__ == "__main__":
    unittest.main()
