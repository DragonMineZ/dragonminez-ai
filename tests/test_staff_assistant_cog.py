import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import staff_assistant as cog_module
from bulmaai.services.mod_actions import ModActionError
from bulmaai.ui import staff_assistant as ui
from v2_helpers import buttons, sent_message, text

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
GUILD_ID = 1


def settings(**overrides):
    values = dict(
        staff_ai_enabled=True, panel_guild_id=GUILD_ID, staff_ai_distill_every=50, panel_public_url="https://panel.example/"
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_member(user_id=42, guild_id=GUILD_ID):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = "Goku"
    member.guild = SimpleNamespace(id=guild_id)
    return member


def make_cog(**setting_overrides):
    bot = MagicMock()
    bot.settings = settings(**setting_overrides)
    bot.get_guild.return_value = SimpleNamespace(id=GUILD_ID)
    return cog_module.StaffAssistantCog(bot)


def make_interaction(user, *, guild_id=GUILD_ID, custom_id="", itype=discord.InteractionType.component, components=None):
    return SimpleNamespace(
        user=user,
        guild_id=guild_id,
        type=itype,
        locale=None,
        client=None,
        data={"custom_id": custom_id, "components": components or []},
        message=None,
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def action_row(**overrides):
    values = dict(
        id=5, user_id=42, guild_id=GUILD_ID, action="warn", args={}, summary="Warn <@9> for spam", origin="discord",
        channel_id=None, message_id=None, created_at=NOW, expires_at=NOW + timedelta(minutes=15), outcome=None,
        outcome_at=None, result=None,
    )
    values.update(overrides)
    return values


def form_components(nickname="Goku", language="auto", timezone_="UTC", ticket="no", consent=True):
    items = [
        {"type": 18, "component": {"type": 4, "custom_id": "nickname", "value": nickname}},
        {"type": 18, "component": {"type": 3, "custom_id": "language", "values": [language]}},
        {"type": 18, "component": {"type": 4, "custom_id": "timezone", "value": timezone_}},
        {"type": 18, "component": {"type": 3, "custom_id": "ticket_info", "values": [ticket]}},
    ]
    if consent:
        items.append({"type": 18, "component": {"type": 3, "custom_id": "consent", "values": ["agree"]}})
    return items


class TimezoneTests(unittest.TestCase):
    def test_parsing(self) -> None:
        self.assertEqual(ui.parse_timezone("America/Sao_Paulo"), ("America/Sao_Paulo", None))
        self.assertEqual(ui.parse_timezone("UTC"), ("UTC", None))
        self.assertEqual(ui.parse_timezone(""), ("UTC", None))
        self.assertEqual(ui.parse_timezone("UTC-3"), ("Etc/GMT+3", None))
        self.assertEqual(ui.parse_timezone("utc+5"), ("Etc/GMT-5", None))
        self.assertEqual(ui.parse_timezone("UTC+0"), ("UTC", None))
        self.assertEqual(ui.parse_timezone("UTC+5:30"), (None, "bad_offset"))
        self.assertEqual(ui.parse_timezone("Mars/Olympus"), (None, "bad_tz"))
        self.assertEqual(ui.parse_timezone("banana"), (None, "bad_tz"))
        self.assertEqual(ui.parse_timezone("UTC+x"), (None, "bad_tz"))


class CustomIdTests(unittest.IsolatedAsyncioTestCase):
    def test_parsing(self) -> None:
        self.assertEqual(ui.parse_custom_id("staffai:confirm:12"), ("confirm", 12))
        self.assertEqual(ui.parse_custom_id("staffai:cancel:3"), ("cancel", 3))
        self.assertIsNone(ui.parse_custom_id("staffai:setup"))
        self.assertIsNone(ui.parse_custom_id("staffai:confirm:x"))
        self.assertEqual(ui.parse_form_id("staffai_form:new"), "new")
        self.assertEqual(ui.parse_form_id("staffai_form:edit"), "edit")
        self.assertIsNone(ui.parse_form_id("staffai_form:other"))

    async def test_confirm_card(self) -> None:
        view = ui.confirm_view(action_row(), "en")
        self.assertIn("Warn", text(view))
        self.assertIn("Expires <t:", text(view))
        self.assertEqual([b.custom_id for b in buttons(view)], ["staffai:confirm:5", "staffai:cancel:5"])


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_helper_ok_non_staff_and_other_guild_denied(self) -> None:
        cog = make_cog()
        member = make_member()
        with patch.object(cog_module, "tier_for", return_value=cog_module.Tier.HELPER):
            self.assertIs(await cog._staff_member(make_interaction(member)), member)
        with patch.object(cog_module, "tier_for", return_value=cog_module.Tier.NONE):
            self.assertIsNone(await cog._staff_member(make_interaction(member)))
        with patch.object(cog_module, "tier_for", return_value=cog_module.Tier.ADMIN):
            self.assertIsNone(await cog._staff_member(make_interaction(make_member(guild_id=2), guild_id=2)))

    async def test_dm_resolves_main_guild_member(self) -> None:
        cog = make_cog()
        member = make_member()
        user = SimpleNamespace(id=42)
        with patch.object(cog_module, "resolve_member", AsyncMock(return_value=member)), patch.object(
            cog_module, "tier_for", return_value=cog_module.Tier.MODERATOR
        ):
            self.assertIs(await cog._staff_member(make_interaction(user, guild_id=None)), member)

    async def test_disabled_denies(self) -> None:
        cog = make_cog(staff_ai_enabled=False)
        with patch.object(cog_module, "tier_for", return_value=cog_module.Tier.ADMIN):
            self.assertIsNone(await cog._staff_member(make_interaction(make_member())))


class FormTests(unittest.IsolatedAsyncioTestCase):
    async def submit(self, components, existing=None, mode="new"):
        cog = make_cog()
        member = make_member()
        interaction = make_interaction(
            member, custom_id=f"staffai_form:{mode}", itype=discord.InteractionType.modal_submit, components=components
        )
        with patch.object(cog_module, "tier_for", return_value=cog_module.Tier.HELPER), patch.object(
            cog_module.staff_store, "get_profile", AsyncMock(return_value=existing)
        ), patch.object(cog_module.staff_store, "save_profile", AsyncMock()) as save:
            await cog.on_interaction(interaction)
        return cog, interaction, save

    async def test_auto_language_saves_none_and_dispatches(self) -> None:
        cog = make_cog()
        member = make_member()
        interaction = make_interaction(
            member, custom_id="staffai_form:new", itype=discord.InteractionType.modal_submit,
            components=form_components(timezone_="UTC-3", ticket="yes"),
        )
        with patch.object(cog_module, "tier_for", return_value=cog_module.Tier.HELPER), patch.object(
            cog_module.staff_store, "get_profile", AsyncMock(return_value=None)
        ), patch.object(cog_module.staff_store, "save_profile", AsyncMock()) as save:
            await cog.on_interaction(interaction)
        save.assert_awaited_once_with(42, nickname="Goku", language=None, timezone="Etc/GMT+3", ticket_info=True)
        self.assertIn("All set, Goku", interaction.response.send_message.await_args.args[0])
        cog.bot.dispatch.assert_called_once_with("staff_onboarded", member)

    async def test_explicit_language_and_edit_does_not_dispatch(self) -> None:
        existing = {"nickname": "Old", "language": None}
        cog, interaction, save = await self.submit(
            form_components(language="pt", consent=False), existing=existing, mode="edit"
        )
        self.assertEqual(save.await_args.kwargs["language"], "pt")
        cog.bot.dispatch.assert_not_called()

    async def test_bad_timezone_and_missing_consent_refused(self) -> None:
        _, interaction, save = await self.submit(form_components(timezone_="UTC+5:30"))
        save.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        _, interaction, save = await self.submit(form_components(consent=False))
        save.assert_not_awaited()


class ConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def click(self, action, row, *, user_id=42, claim=None, execute=None, current=None):
        cog = make_cog()
        member = make_member(user_id)
        interaction = make_interaction(member, custom_id=f"staffai:{action}:5")
        interaction.message = sent_message(ui.confirm_view(row or action_row(), "en"), message_id=77)
        patches = [
            patch.object(cog_module, "tier_for", return_value=cog_module.Tier.HELPER),
            patch.object(cog_module.staff_store, "get_profile", AsyncMock(return_value=None)),
            patch.object(cog_module.staff_store, "get_action", AsyncMock(side_effect=[row, current or row])),
            patch.object(cog_module.staff_store, "claim_action", AsyncMock(return_value=claim)),
            patch.object(cog_module.staff_store, "finish_action", AsyncMock()),
            patch.object(cog_module.staff_store, "release_action", AsyncMock()),
            patch.object(cog_module.staff_store, "cancel_action", AsyncMock(return_value=True)),
            patch.object(cog_module.staff_tools, "execute_action", execute or AsyncMock(return_value="ok")),
        ]
        mocks = [p.start() for p in patches]
        try:
            await cog.on_interaction(interaction)
        finally:
            patch.stopall()
        names = ["tier", "profile", "get", "claim_action", "finish_action", "release_action", "cancel_action", "execute"]
        return interaction, SimpleNamespace(**dict(zip(names, mocks)))

    async def test_wrong_user_refused(self) -> None:
        interaction, store = await self.click("confirm", action_row(), user_id=99)
        self.assertEqual(interaction.response.send_message.await_args.args[0], "This isn't yours.")
        store.claim_action.assert_not_awaited()

    async def test_claim_none_shows_outcome(self) -> None:
        row = action_row(outcome="done", result="did it")
        interaction, store = await self.click("confirm", row, claim=None)
        store.finish_action.assert_not_awaited()
        self.assertEqual(interaction.followup.send.await_args.args[0], "Already done.")

    async def test_success_finishes_done(self) -> None:
        interaction, store = await self.click("confirm", action_row(), claim=action_row(outcome="running"))
        store.finish_action.assert_awaited_once_with(5, "done", "ok")
        card = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIn("✅", text(card))

    async def test_mod_action_error_finishes_failed(self) -> None:
        execute = AsyncMock(side_effect=ModActionError("no_permission"))
        with patch.object(ModActionError, "text", lambda self, language=None: "Nope."):
            interaction, store = await self.click(
                "confirm", action_row(), claim=action_row(outcome="running"), execute=execute
            )
        store.finish_action.assert_awaited_once_with(5, "failed", "Nope.")
        store.release_action.assert_not_awaited()

    async def test_other_error_releases(self) -> None:
        execute = AsyncMock(side_effect=RuntimeError("boom"))
        interaction, store = await self.click(
            "confirm", action_row(), claim=action_row(outcome="running"), execute=execute
        )
        store.release_action.assert_awaited_once_with(5)
        store.finish_action.assert_not_awaited()
        interaction.followup.send.assert_awaited()

    async def test_cancel(self) -> None:
        interaction, store = await self.click("cancel", action_row())
        store.cancel_action.assert_awaited_once_with(5, 42)
        self.assertIn("🚫", text(interaction.response.edit_message.await_args.kwargs["view"]))


class SweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_reminder_claims_before_dm(self) -> None:
        cog = make_cog()
        order = []
        user = SimpleNamespace(id=42, send=AsyncMock(side_effect=lambda *a, **k: order.append("send")))
        cog.bot.get_user.return_value = user

        async def claim(_id):
            order.append("claim")
            return True

        reminder = {"id": 1, "user_id": 42, "text": "call Vegeta", "due_at": NOW, "created_at": NOW}
        with patch.object(cog_module.staff_store, "due_reminders", AsyncMock(return_value=[reminder])), patch.object(
            cog_module.staff_store, "claim_reminder", claim
        ), patch.object(cog_module.staff_store, "get_profile", AsyncMock(return_value={"language": "es"})):
            await cog.send_reminders.coro(cog)
        self.assertEqual(order, ["claim", "send"])
        self.assertEqual(user.send.await_args.args[0], "⏰ call Vegeta")

    async def test_lost_claim_sends_nothing(self) -> None:
        cog = make_cog()
        user = SimpleNamespace(id=42, send=AsyncMock())
        cog.bot.get_user.return_value = user
        reminder = {"id": 1, "user_id": 42, "text": "x", "due_at": NOW, "created_at": NOW}
        with patch.object(cog_module.staff_store, "due_reminders", AsyncMock(return_value=[reminder])), patch.object(
            cog_module.staff_store, "claim_reminder", AsyncMock(return_value=False)
        ):
            await cog.send_reminders.coro(cog)
        user.send.assert_not_awaited()

    async def test_distill_skips_paused_and_isolates_failures(self) -> None:
        cog = make_cog()
        profiles = [
            {"user_id": 1, "consent_at": NOW, "distilled_at": None},
            {"user_id": 2, "consent_at": NOW, "distilled_at": NOW},
        ]
        with patch.object(cog_module.ai_budget, "is_paused", return_value=True), patch.object(
            cog_module.staff_store, "list_profiles", AsyncMock(return_value=profiles)
        ) as listing:
            await cog.distill_all.coro(cog)
        listing.assert_not_awaited()
        distill = AsyncMock(side_effect=[RuntimeError("x"), None])
        with patch.object(cog_module.ai_budget, "is_paused", return_value=False), patch.object(
            cog_module.staff_store, "list_profiles", AsyncMock(return_value=profiles)
        ), patch.object(cog_module.staff_store, "count_since", AsyncMock(return_value=50)), patch.object(
            cog_module.staff_assistant, "distill", distill
        ):
            await cog.distill_all.coro(cog)
        self.assertEqual(distill.await_count, 2)


class ExportTests(unittest.TestCase):
    def test_shape(self) -> None:
        data = json.loads(cog_module.export_payload({"nickname": "G", "consent_at": NOW}, [{"note": "n"}], [], [{"content": "hi"}]))
        self.assertEqual(set(data), {"profile", "notes", "reminders", "vault"})
        self.assertEqual(data["vault"][0]["content"], "hi")
        self.assertIn("2026-11-12", data["profile"]["consent_at"])


if __name__ == "__main__":
    unittest.main()
