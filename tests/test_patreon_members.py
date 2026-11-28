import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bulmaai.cogs import tickets as tickets_module
from bulmaai.cogs.patreon_members import PatreonMembersCog
from bulmaai.cogs.tickets import TicketsCog
from bulmaai.services import ai_tools, patreon_members
from bulmaai.services.patreon_members import PatreonMember
from bulmaai.ui import patreon_check
from test_tickets import ES_ROLE, OWNER_ID, SETTINGS, make_channel, member
from v2_helpers import buttons, text

PATRON_ROLE = 31
ELIGIBLE_TIER = "t-saiyan"


def _member_resource(member_id, user_id, status, tiers=()):
    return {
        "id": member_id,
        "type": "member",
        "attributes": {"patron_status": status, "last_charge_date": "2026-11-20T10:00:00.000+00:00",
                       "last_charge_status": "Paid", "next_charge_date": None},
        "relationships": {
            "user": {"data": {"id": user_id, "type": "user"}},
            "currently_entitled_tiers": {"data": [{"id": tier, "type": "tier"} for tier in tiers]},
        },
    }


def _user(user_id, discord_id):
    connections = {"discord": {"user_id": discord_id}} if discord_id else {"discord": None}
    return {"id": user_id, "type": "user", "attributes": {"social_connections": connections}}


PAGE = {
    "data": [
        _member_resource("m1", "u1", "active_patron", [ELIGIBLE_TIER]),
        _member_resource("m2", "u2", "active_patron", [ELIGIBLE_TIER]),  # no Discord connected
        _member_resource("m3", "u3", None),  # free follower
    ],
    "included": [
        _user("u1", "100"),
        _user("u2", None),
        _user("u3", "300"),
        {"id": ELIGIBLE_TIER, "type": "tier", "attributes": {"title": "Saiyan"}},
    ],
}


def _synced(status="active_patron", tiers=(ELIGIBLE_TIER,)):
    return PatreonMember(
        member_id="m1", discord_user_id=OWNER_ID, patron_status=status, tier_ids=tiers, tier_titles=("Saiyan",),
        last_charge_date=datetime(2026, 11, 20, tzinfo=timezone.utc), last_charge_status="Paid", next_charge_date=None,
    )


class ParseTests(unittest.TestCase):
    def test_keeps_only_pledged_members_with_discord_connected(self):
        members = patreon_members.parse_members_page(PAGE)
        self.assertEqual([(m.member_id, m.discord_user_id) for m in members], [("m1", 100)])
        self.assertEqual(members[0].tier_titles, ("Saiyan",))
        self.assertEqual(members[0].last_charge_status, "Paid")

    def test_entitled_needs_active_status_and_an_eligible_tier(self):
        self.assertTrue(patreon_members.is_entitled(_synced(), (ELIGIBLE_TIER,)))
        self.assertFalse(patreon_members.is_entitled(_synced(status="declined_patron"), (ELIGIBLE_TIER,)))
        self.assertFalse(patreon_members.is_entitled(_synced(tiers=("other",)), (ELIGIBLE_TIER,)))
        self.assertFalse(patreon_members.is_entitled(None, (ELIGIBLE_TIER,)))


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_next_links_until_the_last_page(self):
        first = MagicMock(json=lambda: {**PAGE, "links": {"next": "https://patreon.example/next?cursor=abc"}})
        second = MagicMock(json=lambda: {"data": [], "included": []})
        with patch.object(patreon_members, "request", AsyncMock(side_effect=[first, second])) as request:
            members = await patreon_members.fetch_all_members(creator_token="tok", campaign_id="c1")
        self.assertEqual(len(members), 1)
        self.assertIn("/campaigns/c1/members", request.await_args_list[0].args[1])
        self.assertIn("social_connections", request.await_args_list[0].kwargs["params"]["fields[user]"])
        self.assertEqual(request.await_args_list[1].args[1], "https://patreon.example/next?cursor=abc")
        self.assertIsNone(request.await_args_list[1].kwargs["params"])


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def cog(self):
        return PatreonMembersCog(SimpleNamespace(settings=SimpleNamespace(PATREON_CREATOR_TOKEN="tok", PATREON_CAMPAIGN_ID="c1")))

    async def test_stores_a_full_sync(self):
        with patch.object(patreon_members, "fetch_all_members", AsyncMock(return_value=[_synced()])), \
                patch.object(patreon_members, "replace_all", AsyncMock()) as replace_all:
            self.assertEqual(await self.cog().sync_once(), 1)
        replace_all.assert_awaited_once_with([_synced()])

    async def test_an_empty_or_failed_sync_never_touches_the_stored_list(self):
        for fetch in (AsyncMock(return_value=[]), AsyncMock(side_effect=RuntimeError("503"))):
            with patch.object(patreon_members, "fetch_all_members", fetch), \
                    patch.object(patreon_members, "replace_all", AsyncMock()) as replace_all:
                self.assertIsNone(await self.cog().sync_once())
            replace_all.assert_not_awaited()


class CheckCardTests(unittest.IsolatedAsyncioTestCase):  # py-cord views need a running loop
    def test_diagnosis(self):
        tiers = (ELIGIBLE_TIER,)
        self.assertEqual(patreon_check.diagnose(has_role=True, member=None, eligible_tier_ids=tiers), patreon_check.HAS_ROLE)
        self.assertEqual(patreon_check.diagnose(has_role=False, member=None, eligible_tier_ids=tiers), patreon_check.NOT_FOUND)
        self.assertEqual(patreon_check.diagnose(has_role=False, member=_synced(), eligible_tier_ids=tiers),
                         patreon_check.ENTITLED_NO_ROLE)
        self.assertEqual(patreon_check.diagnose(has_role=False, member=_synced("declined_patron"), eligible_tier_ids=tiers),
                         patreon_check.NOT_ENTITLED)

    async def test_card_names_the_status_and_opens_the_form(self):
        view = patreon_check.check_card(patreon_check.NOT_ENTITLED, _synced("declined_patron"), "es", "ticket_form:patreon:es")
        self.assertIn("tu último pago fue rechazado", text(view))
        self.assertEqual([(b.label, b.custom_id) for b in buttons(view)], [("Aún necesito ayuda", "ticket_form:patreon:es")])
        entitled = patreon_check.check_card(patreon_check.ENTITLED_NO_ROLE, _synced(), "en", "ticket_form:patreon:en")
        self.assertIn("(Saiyan)", text(entitled))


class PanelCheckTests(unittest.IsolatedAsyncioTestCase):
    def cog(self, token="tok"):
        settings = SimpleNamespace(
            **vars(SETTINGS), PATREON_CREATOR_TOKEN=token, patreon_access_role_ids=(PATRON_ROLE,),
            patreon_eligible_tier_ids=(ELIGIBLE_TIER,), patreon_perk_tier_titles=(),
        )
        bot = SimpleNamespace(settings=settings, get_cog=lambda name: None, get_channel=lambda _id: None, user=SimpleNamespace(id=9))
        return TicketsCog(bot)

    def pick(self, user):
        return SimpleNamespace(
            user=user,
            data={"values": ["patreon"]},
            message=SimpleNamespace(flags=discord.MessageFlags(ephemeral=False), edit=AsyncMock()),
            channel=make_channel(),
            response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock()),
        )

    async def test_patreon_pick_shows_the_check_first_in_the_members_language(self):
        interaction = self.pick(member(OWNER_ID, ES_ROLE))
        with patch.object(patreon_members, "has_synced", AsyncMock(return_value=True)), \
                patch.object(patreon_members, "get_member_by_discord", AsyncMock(return_value=_synced())):
            await self.cog().on_panel_select(interaction)
        interaction.response.send_modal.assert_not_awaited()
        sent = interaction.response.send_message.await_args.kwargs
        self.assertIs(sent["ephemeral"], True)
        self.assertIn("patrocinador activo (Saiyan)", text(sent["view"]))
        self.assertEqual([b.custom_id for b in buttons(sent["view"])], ["ticket_form:patreon:es"])
        self.assertEqual(interaction.message.edit.await_count, 1)  # the public panel is still reset

    async def test_members_with_the_role_are_told_so(self):
        interaction = self.pick(member(OWNER_ID, ES_ROLE, PATRON_ROLE))
        with patch.object(patreon_members, "has_synced", AsyncMock(return_value=True)), \
                patch.object(patreon_members, "get_member_by_discord", AsyncMock(return_value=None)):
            await self.cog().on_panel_select(interaction)
        self.assertIn("Ya tienes el rol de Patreon", text(interaction.response.send_message.await_args.kwargs["view"]))

    async def test_without_patreon_data_the_form_opens_as_before(self):
        cases = (
            ("", AsyncMock(return_value=True)),  # no creator token
            ("tok", AsyncMock(return_value=False)),  # never synced
            ("tok", AsyncMock(side_effect=RuntimeError("db down"))),
        )
        for token, has_synced in cases:
            interaction = self.pick(member(OWNER_ID, ES_ROLE))
            with patch.object(patreon_members, "has_synced", has_synced), \
                    patch.object(patreon_members, "get_member_by_discord", AsyncMock(return_value=None)):
                await self.cog(token).on_panel_select(interaction)
            modal = interaction.response.send_modal.await_args.args[0]
            self.assertEqual((modal.category.key, modal.language), ("patreon", "es"))
            interaction.response.send_message.assert_not_awaited()

    async def test_other_categories_skip_the_check(self):
        interaction = self.pick(member(OWNER_ID, ES_ROLE))
        interaction.data = {"values": ["bug"]}
        with patch.object(tickets_module.patreon_members, "has_synced", AsyncMock()) as has_synced:
            await self.cog().on_panel_select(interaction)
        has_synced.assert_not_awaited()
        interaction.response.send_modal.assert_awaited_once()


class StatusToolTests(unittest.TestCase):
    def test_membership_summary(self):
        settings = SimpleNamespace(patreon_eligible_tier_ids=(ELIGIBLE_TIER,))
        self.assertEqual(ai_tools._membership_summary(None, settings), {"discord_connected_in_patreon": False})
        self.assertEqual(
            ai_tools._membership_summary(_synced(), settings),
            {"discord_connected_in_patreon": True, "patron_status": "active_patron", "tiers": ["Saiyan"],
             "entitled_to_patron_role": True, "last_charge_status": "Paid"},
        )
