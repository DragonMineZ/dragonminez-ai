import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from bulmaai.cogs.tickets import TicketsCog
from bulmaai.services import patreon_members, patreon_perks
from bulmaai.services.patreon_perks import PerkClaim
from bulmaai.ui import patreon_perks as perks_ui
from test_patreon_members import ELIGIBLE_TIER, PATRON_ROLE, _synced
from test_tickets import ES_ROLE, OWNER_ID, SETTINGS, make_channel, member
from v2_helpers import buttons, text

YUSEIX = 555


def _cog():
    settings = SimpleNamespace(
        **vars(SETTINGS), PATREON_CREATOR_TOKEN="tok", patreon_access_role_ids=(PATRON_ROLE,),
        patreon_eligible_tier_ids=(ELIGIBLE_TIER,), patreon_perk_tier_titles=("Saiyan",), patreon_perk_user_ids=(YUSEIX,),
    )
    bot = SimpleNamespace(settings=settings, get_cog=lambda name: None, get_channel=lambda _id: None, user=SimpleNamespace(id=9))
    return TicketsCog(bot)


def _claim(perk, status="pending"):
    return PerkClaim(claim_id=1, discord_user_id=OWNER_ID, perk=perk, status=status, ticket_channel_id=900,
                     created_at=datetime(2026, 11, 27, tzinfo=timezone.utc))


def _click(language="es"):
    return SimpleNamespace(
        user=member(OWNER_ID, ES_ROLE),
        data={"custom_id": f"patreon_perk:{language}"},
        response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock()),
    )


class EligibilityTests(unittest.TestCase):
    def test_needs_an_active_paid_patron_on_a_perk_tier(self):
        self.assertTrue(patreon_perks.can_claim(_synced(), ("saiyan",)))  # case-insensitive titles
        self.assertFalse(patreon_perks.can_claim(_synced(), ("Benefactor",)))
        self.assertFalse(patreon_perks.can_claim(_synced("declined_patron"), ("Saiyan",)))
        self.assertFalse(patreon_perks.can_claim(replace(_synced(), last_charge_status="Pending"), ("Saiyan",)))
        self.assertFalse(patreon_perks.can_claim(None, ("Saiyan",)))

    def test_unknown_perks_are_refused_by_the_store(self):
        with self.assertRaises(ValueError):
            asyncio.run(patreon_perks.claim(discord_user_id=1, perk="spaceship", minecraft_username="x", details="y"))


class CheckCardButtonTests(unittest.IsolatedAsyncioTestCase):
    async def pick(self, synced):
        interaction = SimpleNamespace(
            user=member(OWNER_ID, ES_ROLE),
            data={"values": ["patreon"]},
            message=SimpleNamespace(flags=discord.MessageFlags(ephemeral=False), edit=AsyncMock()),
            channel=make_channel(),
            response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock()),
        )
        with patch.object(patreon_members, "has_synced", AsyncMock(return_value=True)), \
                patch.object(patreon_members, "get_member_by_discord", AsyncMock(return_value=synced)):
            await _cog().on_panel_select(interaction)
        return [b.custom_id for b in buttons(interaction.response.send_message.await_args.kwargs["view"])]

    async def test_perk_tier_patrons_get_the_claim_button_first(self):
        self.assertEqual(await self.pick(_synced()), ["patreon_perk:es", "ticket_form:patreon:es"])

    async def test_others_dont(self):
        self.assertEqual(await self.pick(_synced("former_patron")), ["ticket_form:patreon:es"])


class ClaimButtonTests(unittest.IsolatedAsyncioTestCase):
    async def click(self, synced, claims):
        interaction = _click()
        with patch.object(patreon_members, "get_member_by_discord", AsyncMock(return_value=synced)), \
                patch.object(patreon_perks, "list_claims", AsyncMock(return_value=claims)):
            await _cog().on_perk_claim(interaction)
        return interaction

    async def test_the_form_offers_only_unclaimed_perks(self):
        interaction = await self.click(_synced(), [_claim("clothing")])
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual([o.value for o in modal.perk_select.options], ["hairstyle", "npc_skin"])
        self.assertEqual(modal.title, "Reclamar un beneficio de Patreon")

    async def test_not_eligible_is_told_why(self):
        interaction = await self.click(_synced("declined_patron"), [])
        interaction.response.send_modal.assert_not_awaited()
        self.assertIn("patrocinadores activos", interaction.response.send_message.await_args.args[0])

    async def test_everything_claimed_lists_the_claims(self):
        claims = [_claim("clothing", "accepted"), _claim("hairstyle"), _claim("npc_skin", "delivered")]
        interaction = await self.click(_synced(), claims)
        reply = interaction.response.send_message.await_args.args[0]
        self.assertIn("Ropa personalizada (una vez) (en progreso)", reply)
        self.assertIn("(entregado)", reply)


class SubmitTests(unittest.IsolatedAsyncioTestCase):
    async def submit(self, *, synced=None, claim_id=7, channel="made"):
        cog = _cog()
        ticket_channel = SimpleNamespace(id=900, send=AsyncMock())
        cog.create_ticket = AsyncMock(return_value=ticket_channel if channel == "made" else None)
        interaction = SimpleNamespace(
            user=member(OWNER_ID, ES_ROLE),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        reference = SimpleNamespace(to_file=AsyncMock(return_value="file"))
        with patch.object(patreon_members, "get_member_by_discord", AsyncMock(return_value=synced or _synced())), \
                patch.object(patreon_perks, "claim", AsyncMock(return_value=claim_id)) as claim, \
                patch.object(patreon_perks, "attach_ticket", AsyncMock()) as attach, \
                patch.object(patreon_perks, "release", AsyncMock()) as release:
            await cog.submit_perk_claim(
                interaction, perk="hairstyle", minecraft_username="Goku", details="spiky, blue",
                references=[reference], language="es",
            )
        return SimpleNamespace(cog=cog, interaction=interaction, channel=ticket_channel, claim=claim, attach=attach, release=release)

    async def test_a_claim_opens_a_team_only_ticket_and_pings_the_makers(self):
        done = await self.submit()
        args, kwargs = done.cog.create_ticket.await_args
        self.assertEqual(args[1].key, "patreon")
        self.assertEqual(args[2], [("Perk", "Custom hairstyle (one time)"), ("Minecraft username", "Goku"), ("Details", "spiky, blue")])
        self.assertEqual((kwargs["files"], kwargs["language"], kwargs["team_only"]), (["file"], "es", True))
        done.attach.assert_awaited_once_with(7, 900)
        ping = done.channel.send.await_args.kwargs
        self.assertIn(f"<@{YUSEIX}>", text(ping["view"]))
        self.assertEqual([u.id for u in ping["allowed_mentions"].users], [YUSEIX])
        self.assertFalse(ping["allowed_mentions"].everyone)
        self.assertFalse(ping["allowed_mentions"].roles)

    async def test_an_already_claimed_perk_opens_nothing(self):
        done = await self.submit(claim_id=None)
        done.cog.create_ticket.assert_not_awaited()
        self.assertEqual(done.interaction.edit_original_response.await_args.kwargs["content"], "Ya reclamaste ese beneficio.")

    async def test_eligibility_is_checked_again_on_submit(self):
        done = await self.submit(synced=_synced("declined_patron"))
        done.claim.assert_not_awaited()
        done.cog.create_ticket.assert_not_awaited()

    async def test_a_ticket_that_cant_open_gives_the_claim_back(self):
        done = await self.submit(channel=None)
        done.release.assert_awaited_once_with(7)
        done.attach.assert_not_awaited()


class ButtonIdTests(unittest.TestCase):
    def test_language_round_trips(self):
        self.assertEqual(perks_ui.parse_language("patreon_perk:pt"), "pt")
        self.assertIsNone(perks_ui.parse_language("ticket_form:patreon:pt"))
