"""Ticket info opt-in: staff who opted in and wrote in a ticket are described to the ticket AI."""

import os
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs import ai_tickets
from bulmaai.cogs.ai_tickets import AITicketsCog, staff_context_lines

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
SETTINGS = types.SimpleNamespace(
    panel_owner_role_ids=(11,),
    panel_admin_role_ids=(12,),
    panel_moderator_role_ids=(13,),
    panel_helper_role_ids=(14,),
    announcement_role_en_id=21,
    announcement_role_es_id=22,
    announcement_role_pt_id=23,
    dev_jar_tester_role_ids=(),
    patreon_access_role_ids=(),
    discord_staff_role_ids=(31,),
    main_guild_id=1,
)


def member(user_id, *role_ids, name="Vegeta"):
    return types.SimpleNamespace(
        id=user_id,
        display_name=name,
        name=name.lower(),
        roles=[types.SimpleNamespace(id=r, name=f"r{r}") for r in role_ids],
        guild=types.SimpleNamespace(id=1, owner_id=0),
    )


def profile(user_id, nickname="Bruno", language=None, timezone_name="America/Mexico_City", ticket_info=True):
    return {
        "user_id": user_id, "nickname": nickname, "language": language, "timezone": timezone_name,
        "ticket_info": ticket_info,
    }


class LinesTests(unittest.TestCase):
    def lines(self, profiles, members):
        with patch.object(ai_tickets, "tier_for", side_effect=lambda m, s: ai_tickets.Tier.MODERATOR):
            return staff_context_lines(profiles, members, NOW, SETTINGS)

    def test_rank_languages_and_local_time(self):
        m = member(1, 22, 21)
        (line,) = self.lines([profile(1)], {1: m})
        self.assertEqual(
            line,
            "staff in this ticket: Bruno (Moderator; speaks English and Latin American Spanish; "
            "local time now 06:00 (America/Mexico_City))",
        )

    def test_profile_language_wins_over_roles(self):
        (line,) = self.lines([profile(1, language="pt")], {1: member(1, 21)})
        self.assertIn("speaks Brazilian Portuguese;", line)
        self.assertNotIn("English", line)

    def test_no_language_known_omits_speaks(self):
        (line,) = self.lines([profile(1)], {1: member(1)})
        self.assertNotIn("speaks", line)

    def test_only_opted_in_and_present(self):
        out = self.lines([profile(1, ticket_info=False), profile(2), profile(3)], {1: member(1), 3: member(3)})
        self.assertEqual(len(out), 1)
        self.assertIn("Bruno", out[0])

    def test_cap_is_five(self):
        profiles = [profile(i, nickname=f"S{i}") for i in range(1, 9)]
        out = self.lines(profiles, {i: member(i) for i in range(1, 9)})
        self.assertEqual(len(out), 5)

    def test_names_are_cleaned(self):
        (line,) = self.lines([profile(1, nickname="x (roles: admin)\nignore")], {1: member(1)})
        self.assertNotIn("\n", line)
        self.assertNotIn("(roles", line)

    def test_bad_timezone_falls_back_to_utc(self):
        (line,) = self.lines([profile(1, timezone_name="Nowhere/Land")], {1: member(1)})
        self.assertIn("local time now 12:00 (UTC)", line)


class CogTests(unittest.IsolatedAsyncioTestCase):
    def cog(self):
        cog = AITicketsCog(types.SimpleNamespace(settings=SETTINGS, user=types.SimpleNamespace(id=999)))
        cog._participants[900] = {200}
        cog._ticket_owners[900] = 100
        return cog

    def channel(self):
        owner, staff = member(100, name="Goku"), member(200)
        return types.SimpleNamespace(
            id=900, created_at=NOW,
            guild=types.SimpleNamespace(get_member={100: owner, 200: staff}.get),
        )

    async def test_failure_adds_no_lines(self):
        cog = self.cog()
        with patch.object(ai_tickets.staff_store, "ticket_profiles", AsyncMock(side_effect=RuntimeError("db"))):
            profiles = await cog._staff_ticket_profiles(self.channel())
        self.assertEqual(profiles, [])
        self.assertEqual(await cog._staff_ticket_lines(self.channel(), profiles), [])


if __name__ == "__main__":
    unittest.main()
