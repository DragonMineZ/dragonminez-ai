import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import discord


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.power_level import XP_PER_MESSAGE, PowerLevelCog
from bulmaai.services.member_activity import (
    level_for_xp,
    parse_role_reward_map,
    xp_threshold,
)
from bulmaai.ui.power_level_views import build_leaderboard_card, build_power_level_card
from v2_helpers import text


class LevelCurveTests(unittest.TestCase):
    def test_xp_threshold_is_quadratic(self) -> None:
        self.assertEqual(xp_threshold(0), 0)
        self.assertEqual(xp_threshold(1), 50)
        self.assertEqual(xp_threshold(2), 200)
        self.assertEqual(xp_threshold(3), 450)

    def test_level_for_xp_matches_thresholds_at_boundaries(self) -> None:
        self.assertEqual(level_for_xp(0), 0)
        self.assertEqual(level_for_xp(49), 0)
        self.assertEqual(level_for_xp(50), 1)
        self.assertEqual(level_for_xp(199), 1)
        self.assertEqual(level_for_xp(200), 2)
        self.assertEqual(level_for_xp(449), 2)
        self.assertEqual(level_for_xp(450), 3)

    def test_level_for_xp_rejects_negative_xp(self) -> None:
        self.assertEqual(level_for_xp(-100), 0)


class RoleRewardMapTests(unittest.TestCase):
    def test_parses_valid_json_object(self) -> None:
        self.assertEqual(
            parse_role_reward_map('{"5": 111, "10": 222}'),
            {5: 111, 10: 222},
        )

    def test_empty_or_missing_value_yields_empty_map(self) -> None:
        self.assertEqual(parse_role_reward_map(""), {})
        self.assertEqual(parse_role_reward_map("{}"), {})

    def test_malformed_json_yields_empty_map(self) -> None:
        self.assertEqual(parse_role_reward_map("not json"), {})

    def test_non_object_json_yields_empty_map(self) -> None:
        self.assertEqual(parse_role_reward_map("[1, 2, 3]"), {})

    def test_skips_entries_that_do_not_coerce_to_ints(self) -> None:
        self.assertEqual(
            parse_role_reward_map('{"5": 111, "oops": 222, "7": "not-an-id"}'),
            {5: 111},
        )


class PowerLevelCardTests(unittest.IsolatedAsyncioTestCase):  # py-cord views need a running loop
    async def test_power_level_card_reports_progress_to_next_level(self) -> None:
        view = build_power_level_card(
            display_name="Goku",
            avatar_url="https://example.test/avatar.png",
            xp=125,
            level=1,
        )
        card_text = text(view)

        self.assertIn("## ⚡ Goku's Power Level", card_text)
        self.assertIn("**Level** 1　**Total XP** 125", card_text)
        self.assertIn("`▰▰▰▰▰▱▱▱▱▱` 75 / 150 XP (50%)", card_text)
        self.assertIn("-# to level 2", card_text)

    async def test_leaderboard_card_orders_and_ranks_entries(self) -> None:
        card_text = text(build_leaderboard_card(guild_name="DragonMineZ", entries=[("Goku", 500, 3), ("Vegeta", 300, 2)]))

        self.assertIn("🥇 Goku · Level 3 (500 XP)", card_text)
        self.assertLess(card_text.index("Goku"), card_text.index("Vegeta"))

    async def test_leaderboard_card_handles_no_entries(self) -> None:
        self.assertIn("No one has powered up yet.", text(build_leaderboard_card(guild_name="DragonMineZ", entries=[])))


def _fake_member(*, user_id: int, guild, bot: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = bot
    member.guild = guild
    member.add_roles = AsyncMock()
    return member


class PowerLevelCooldownTests(unittest.TestCase):
    def test_cooldown_blocks_within_window_and_clears_after(self) -> None:
        cog = PowerLevelCog.__new__(PowerLevelCog)
        cog._last_award = {}

        self.assertFalse(cog._on_cooldown(1, 2, now=100.0))
        self.assertTrue(cog._on_cooldown(1, 2, now=130.0))
        self.assertFalse(cog._on_cooldown(1, 2, now=161.0))


class PowerLevelRoleRewardTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, role_rewards: str) -> PowerLevelCog:
        cog = PowerLevelCog.__new__(PowerLevelCog)
        cog.bot = SimpleNamespace(settings=SimpleNamespace(power_level_role_rewards=role_rewards))
        return cog

    async def test_grants_a_role_for_each_level_crossed(self) -> None:
        role5 = SimpleNamespace(id=555)
        role10 = SimpleNamespace(id=1010)
        guild = SimpleNamespace(id=1, get_role=lambda rid: {555: role5, 1010: role10}.get(rid))
        member = SimpleNamespace(id=9, guild=guild, add_roles=AsyncMock())
        cog = self._cog('{"5": 555, "10": 1010}')

        await cog._grant_role_rewards(member, previous_level=4, new_level=10)

        self.assertEqual(
            member.add_roles.call_args_list,
            [
                call(role5, reason="Reached power level 5"),
                call(role10, reason="Reached power level 10"),
            ],
        )

    async def test_role_missing_from_guild_is_skipped(self) -> None:
        guild = SimpleNamespace(id=1, get_role=lambda rid: None)
        member = SimpleNamespace(id=9, guild=guild, add_roles=AsyncMock())
        cog = self._cog('{"1": 999}')

        await cog._grant_role_rewards(member, previous_level=0, new_level=1)

        member.add_roles.assert_not_called()

    async def test_empty_role_map_short_circuits(self) -> None:
        member = SimpleNamespace(id=9, guild=SimpleNamespace(id=1), add_roles=AsyncMock())
        cog = self._cog("{}")

        await cog._grant_role_rewards(member, previous_level=0, new_level=5)

        member.add_roles.assert_not_called()


class PowerLevelMessageAwardTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, **settings_overrides) -> PowerLevelCog:
        cog = PowerLevelCog.__new__(PowerLevelCog)
        defaults = dict(
            power_level_enabled=True,
            power_level_excluded_channel_ids=(),
            power_level_role_rewards="{}",
        )
        defaults.update(settings_overrides)
        cog.bot = SimpleNamespace(settings=SimpleNamespace(**defaults))
        cog._last_award = {}
        return cog

    async def test_ignores_bot_authors(self) -> None:
        cog = self._cog()
        guild = SimpleNamespace(id=1)
        message = SimpleNamespace(
            author=_fake_member(user_id=1, guild=guild, bot=True),
            guild=guild,
            channel=SimpleNamespace(id=1),
        )

        with patch("bulmaai.cogs.power_level.award_xp", new=AsyncMock()) as award_mock:
            await cog._award_message_xp(message)

        award_mock.assert_not_called()

    async def test_ignores_dms(self) -> None:
        cog = self._cog()
        message = SimpleNamespace(
            author=_fake_member(user_id=1, guild=None),
            guild=None,
            channel=SimpleNamespace(id=1),
        )

        with patch("bulmaai.cogs.power_level.award_xp", new=AsyncMock()) as award_mock:
            await cog._award_message_xp(message)

        award_mock.assert_not_called()

    async def test_ignores_excluded_channel(self) -> None:
        cog = self._cog(power_level_excluded_channel_ids=(555,))
        guild = SimpleNamespace(id=1)
        message = SimpleNamespace(
            author=_fake_member(user_id=1, guild=guild),
            guild=guild,
            channel=SimpleNamespace(id=555),
        )

        with patch("bulmaai.cogs.power_level.award_xp", new=AsyncMock()) as award_mock:
            await cog._award_message_xp(message)

        award_mock.assert_not_called()

    async def test_second_message_within_cooldown_is_not_awarded(self) -> None:
        cog = self._cog()
        guild = SimpleNamespace(id=1)
        message = SimpleNamespace(
            author=_fake_member(user_id=9, guild=guild),
            guild=guild,
            channel=SimpleNamespace(id=1),
        )

        with patch("bulmaai.cogs.power_level.award_xp", new=AsyncMock(return_value=15)) as award_mock:
            await cog._award_message_xp(message)
            await cog._award_message_xp(message)

        award_mock.assert_called_once_with(1, 9, XP_PER_MESSAGE)

    async def test_level_up_persists_level_and_grants_reward_role(self) -> None:
        cog = self._cog(power_level_role_rewards='{"1": 777}')
        role = SimpleNamespace(id=777)
        guild = SimpleNamespace(id=1, get_role=lambda rid: role if rid == 777 else None)
        author = _fake_member(user_id=9, guild=guild)
        message = SimpleNamespace(author=author, guild=guild, channel=SimpleNamespace(id=1))

        with (
            patch("bulmaai.cogs.power_level.award_xp", new=AsyncMock(return_value=50)),
            patch("bulmaai.cogs.power_level.set_level", new=AsyncMock()) as set_level_mock,
        ):
            await cog._award_message_xp(message)

        set_level_mock.assert_called_once_with(1, 9, 1)
        author.add_roles.assert_called_once_with(role, reason="Reached power level 1")

    async def test_no_level_change_skips_persistence(self) -> None:
        cog = self._cog()
        guild = SimpleNamespace(id=1)
        author = _fake_member(user_id=9, guild=guild)
        message = SimpleNamespace(author=author, guild=guild, channel=SimpleNamespace(id=1))

        with (
            patch("bulmaai.cogs.power_level.award_xp", new=AsyncMock(return_value=15)),
            patch("bulmaai.cogs.power_level.set_level", new=AsyncMock()) as set_level_mock,
        ):
            await cog._award_message_xp(message)

        set_level_mock.assert_not_called()
        author.add_roles.assert_not_called()


if __name__ == "__main__":
    unittest.main()
