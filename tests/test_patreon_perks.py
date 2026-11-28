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


class ClaimCardTests(unittest.IsolatedAsyncioTestCase):  # py-cord views need a running loop
    async def test_waiting_card_pings_the_makers_with_accept_and_reject(self):
        view = perks_ui.claim_card(perk="hairstyle", claim_id=7, status="pending", makers=[YUSEIX])
        self.assertIn(f"<@{YUSEIX}> this one is yours", text(view))
        self.assertEqual([b.custom_id for b in buttons(view)], ["perk_claim:accept:7", "perk_claim:reject:7"])

    async def test_decided_cards_say_who_and_have_no_buttons(self):
        accepted = perks_ui.claim_card(perk="npc_skin", claim_id=7, status="accepted", makers=[YUSEIX],
                                       decided_by=YUSEIX, notion_url="https://notion.example/p")
        self.assertIn(f"✅ Accepted by <@{YUSEIX}> · [Tracker item](https://notion.example/p)", text(accepted))
        self.assertEqual(buttons(accepted), [])
        failed = perks_ui.claim_card(perk="hairstyle", claim_id=7, status="accepted", makers=[], decided_by=1, notion_failed=True)
        self.assertIn("add it by hand", text(failed))
        rejected = perks_ui.claim_card(perk="hairstyle", claim_id=7, status="rejected", makers=[], decided_by=1)
        self.assertIn("can claim this perk again", text(rejected))

    def test_decision_ids_round_trip(self):
        self.assertEqual(perks_ui.parse_decision("perk_claim:accept:7"), ("accept", 7))
        for bad in ("perk_claim:approve:7", "perk_claim:accept:x", "patreon_perk:es"):
            self.assertIsNone(perks_ui.parse_decision(bad))


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    async def decide(self, user, action="accept", decided="fresh", notion_token="ntok"):
        from test_tickets import click, make_ticket
        from bulmaai.cogs import tickets as cog_module

        cog = _cog()
        cog.settings.notion_token = notion_token
        cog.settings.notion_tracker_data_source_id = "ds"
        card = perks_ui.claim_card(perk="hairstyle", claim_id=7, status="pending", makers=[YUSEIX])
        interaction = click(user, f"perk_claim:{action}:7", card)
        interaction.guild = SimpleNamespace(get_member=lambda _id: "Bel#0001")
        interaction.guild_id = 1
        interaction.channel = SimpleNamespace(send=AsyncMock())
        status = "accepted" if action == "accept" else "rejected"
        claim = replace(_claim("hairstyle", status), claim_id=7, decided_by=user.id, minecraft_username="Goku",
                        details="spiky, blue")
        with patch.object(patreon_perks, "decide", AsyncMock(return_value=claim if decided == "fresh" else None)) as decide, \
                patch.object(patreon_perks, "get_claim", AsyncMock(return_value=replace(claim, decided_by=999))), \
                patch.object(patreon_perks, "set_notion_page", AsyncMock()) as set_page, \
                patch.object(cog_module.notion, "create_page", AsyncMock(return_value={"id": "p1", "url": "https://notion.example/p1"})) as create, \
                patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(language="es"))):
            await cog.on_interaction(interaction)
        return SimpleNamespace(interaction=interaction, decide=decide, create=create, set_page=set_page)

    async def test_others_cant_decide(self):
        done = await self.decide(member(42))
        done.decide.assert_not_awaited()
        done.interaction.response.send_message.assert_awaited_once()

    async def test_accept_adds_a_tracker_item_and_tells_the_member(self):
        done = await self.decide(member(YUSEIX))
        done.decide.assert_awaited_once_with(7, "accepted", YUSEIX)
        body = done.create.await_args.args[1]
        self.assertEqual(body["properties"]["Name"]["title"][0]["text"]["content"], "Custom hairstyle for Goku")
        self.assertEqual(body["properties"]["Status"], {"status": {"name": "Backlog"}})
        self.assertEqual(body["properties"]["Area"], {"multi_select": [{"name": "Patreon"}]})
        self.assertEqual(body["children"][-1]["bookmark"]["url"], "https://discord.com/channels/1/900")
        done.set_page.assert_awaited_once_with(7, "p1")
        edited = done.interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIn("[Tracker item](https://notion.example/p1)", text(edited))
        self.assertIn("El equipo aceptó tu solicitud", done.interaction.channel.send.await_args.args[0])

    async def test_reject_frees_the_perk_without_notion(self):
        done = await self.decide(member(YUSEIX), action="reject")
        done.create.assert_not_awaited()
        self.assertIn("Rejected by", text(done.interaction.edit_original_response.await_args.kwargs["view"]))

    async def test_a_second_click_only_shows_the_first_decision(self):
        done = await self.decide(member(YUSEIX), decided="already")
        done.create.assert_not_awaited()
        self.assertIn("Accepted by <@999>", text(done.interaction.edit_original_response.await_args.kwargs["view"]))
        done.interaction.channel.send.assert_not_awaited()
