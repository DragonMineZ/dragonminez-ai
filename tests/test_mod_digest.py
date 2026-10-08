import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import mod_digest as digest_cog
from bulmaai.services import mod_digest
from bulmaai.services.automod_hits import FilterStats
from bulmaai.services.mod_digest import (
    ActionCount,
    ActorCount,
    AppealStats,
    DigestData,
    ModeratorTotal,
    RepeatOffender,
    ScamImageStats,
)

NOW = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)  # a Monday
TUESDAY = datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc)
HELPER_ROLE, MOD_ROLE, ADMIN_ROLE = 1, 2, 3
OWNER_ID, HELPER_ID, MOD_ID, ADMIN_ID = 111, 222, 444, 666


def fake_settings(**overrides):
    base = dict(
        panel_admin_role_ids=(ADMIN_ROLE,),
        panel_moderator_role_ids=(MOD_ROLE,),
        panel_helper_role_ids=(HELPER_ROLE,),
        panel_owner_role_ids=(),
        moderation_digest_enabled=True,
        moderation_digest_channel_id=None,
        panel_guild_id=1,
        dev_guild_id=None,
        moderation_scam_images_enforce=False,
        moderation_caps_percent=70,
        moderation_zalgo_enabled=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def empty_data(**overrides) -> DigestData:
    base = dict(
        guild_id=1,
        period_start=datetime(2025, 12, 29, tzinfo=timezone.utc),
        period_end=NOW,
        action_counts=(),
        actor_counts=(),
        top_moderators=(),
        repeat_offenders=(),
        filter_stats=(),
        unreviewed_automod_hits=0,
        scam_images=ScamImageStats(0, 0, 0, 0),
        appeals=AppealStats(0, 0, 0),
        reports_received=0,
        antiraid_actions=0,
    )
    base.update(overrides)
    return DigestData(**base)


def fields_by_name(card: mod_digest.DigestCard) -> dict[str, str]:
    return dict(card.fields)


class BuildEmbedsTests(unittest.TestCase):
    def test_deltas_render_arrows_and_omit_when_unchanged(self):
        data = empty_data(
            action_counts=(
                ActionCount("warn", 12, 9),
                ActionCount("kick", 5, 5),
                ActionCount("ban", 2, 6),
            )
        )
        (card,) = mod_digest.build_cards(data, fake_settings())
        value = fields_by_name(card)["Case actions"]
        self.assertIn("**warn**: 12 (▲3)", value)
        self.assertIn("**kick**: 5", value)
        self.assertNotIn("**kick**: 5 (", value)  # unchanged: no delta parenthetical
        self.assertIn("**ban**: 2 (▼4)", value)

    def test_empty_sections_are_omitted_entirely(self):
        cards = mod_digest.build_cards(empty_data(), fake_settings())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].fields, [])
        self.assertIn("No moderation activity", cards[0].note)

    def test_populated_sections_appear_in_order_and_add_a_second_card(self):
        data = empty_data(
            action_counts=(ActionCount("warn", 3, 1),),
            actor_counts=(ActorCount(True, 3, 1), ActorCount(False, 1, 2)),
            top_moderators=(ModeratorTotal(555, 3),),
            repeat_offenders=(RepeatOffender(777, 4),),
            appeals=AppealStats(2, 1, 1),
            reports_received=1,
            antiraid_actions=2,
            filter_stats=(FilterStats("excessive_caps", hits=10, confirmed=1, false_positives=4),),
            unreviewed_automod_hits=3,
        )
        overview, automod = mod_digest.build_cards(data, fake_settings())
        self.assertEqual(
            [name for name, _ in overview.fields],
            [
                "Case actions",
                "Who acted",
                "Top moderators",
                "Repeat offenders (3+ cases)",
                "Appeals",
                "Reports",
                "Anti-raid actions",
            ],
        )
        overview_values = fields_by_name(overview)
        self.assertIn("Human staff: 3 (▲2)", overview_values["Who acted"])
        self.assertIn("Automatic: 1 (▼1)", overview_values["Who acted"])
        self.assertIn("<@555>: 3", overview_values["Top moderators"])
        self.assertIn("<@777>: 4", overview_values["Repeat offenders (3+ cases)"])
        self.assertIn("Received: 2", overview_values["Appeals"])
        self.assertEqual(overview_values["Reports"], "1")
        self.assertEqual(overview_values["Anti-raid actions"], "2")

        automod_values = fields_by_name(automod)
        self.assertIn("excessive_caps", automod_values["Automod hits by filter"])
        self.assertEqual(automod_values["Unreviewed automod hits"], "3")
        self.assertIn("moderation_caps_percent", automod_values["Tuning suggestions"])

    def test_suggestions_render_setting_form_and_note_form(self):
        data = empty_data(
            filter_stats=(
                FilterStats("excessive_caps", hits=10, confirmed=1, false_positives=4),
                FilterStats("banned_word", hits=5, confirmed=0, false_positives=3),
            )
        )
        _, automod = mod_digest.build_cards(data, fake_settings(moderation_caps_percent=70))
        value = fields_by_name(automod)["Tuning suggestions"]
        self.assertIn("moderation_caps_percent 70 → 80", value)
        self.assertIn("moderation_banned_words", value)

    def test_scam_image_section_shows_mode_counts_and_list_size(self):
        data = empty_data(
            filter_stats=(FilterStats("scam_image", hits=10, confirmed=6, false_positives=2),),
            scam_images=ScamImageStats(matches=10, confirmed=6, false_positives=2, list_size=42),
        )

        _, shadow = mod_digest.build_cards(data, fake_settings(moderation_scam_images_enforce=False))
        shadow_value = fields_by_name(shadow)["Scam images"]
        self.assertIn("Mode: shadow — review only", shadow_value)
        self.assertIn("Matches this week: 10", shadow_value)
        self.assertIn("Reviewed: 8/10", shadow_value)
        self.assertIn("Known scam images: 42", shadow_value)
        # scam_image gets its own section, not a duplicate row in the generic filter list
        self.assertNotIn("Automod hits by filter", fields_by_name(shadow))

        _, enforcing = mod_digest.build_cards(data, fake_settings(moderation_scam_images_enforce=True))
        self.assertIn("Mode: enforcing", fields_by_name(enforcing)["Scam images"])

    def test_limits_are_respected_with_many_rows(self):
        offenders = tuple(RepeatOffender(user_id=1000 + i, count=50 - i) for i in range(200))
        filters = tuple(FilterStats(f"reason_{i}", hits=100, confirmed=10, false_positives=5) for i in range(40))
        data = empty_data(repeat_offenders=offenders, filter_stats=filters)
        overview, automod = mod_digest.build_cards(data, fake_settings())

        repeat_value = fields_by_name(overview)["Repeat offenders (3+ cases)"]
        self.assertLessEqual(len(repeat_value), 1024)
        self.assertIn("more", repeat_value)

        filter_value = fields_by_name(automod)["Automod hits by filter"]
        self.assertLessEqual(len(filter_value), 1024)
        self.assertIn("more", filter_value)

        for _, value in list(overview.fields) + list(automod.fields):
            self.assertLessEqual(len(value), 1024)


class ChannelResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_channel_id_wins(self):
        bot = SimpleNamespace(settings=fake_settings(moderation_digest_channel_id=999))
        guild = SimpleNamespace()  # no text_channels: the fallback branch must not touch it
        fake_channel = SimpleNamespace(id=999)
        with patch(
            "bulmaai.services.mod_actions.resolve_channel", AsyncMock(return_value=fake_channel)
        ) as resolve:
            channel = await digest_cog.resolve_digest_channel(bot, guild)
        resolve.assert_awaited_once_with(bot, 999)
        self.assertIs(channel, fake_channel)

    async def test_falls_back_to_staff_general_by_name(self):
        bot = SimpleNamespace(settings=fake_settings(moderation_digest_channel_id=None))
        general = SimpleNamespace(name="general")
        staff_general = SimpleNamespace(name="staff-general")
        guild = SimpleNamespace(text_channels=[general, staff_general])
        channel = await digest_cog.resolve_digest_channel(bot, guild)
        self.assertIs(channel, staff_general)

    async def test_neither_configured_nor_found_returns_none(self):
        bot = SimpleNamespace(settings=fake_settings(moderation_digest_channel_id=None))
        guild = SimpleNamespace(text_channels=[SimpleNamespace(name="general")])
        self.assertIsNone(await digest_cog.resolve_digest_channel(bot, guild))


class LoopBodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guild = SimpleNamespace(id=1, text_channels=[])
        self.bot = SimpleNamespace(
            settings=fake_settings(),
            get_guild=lambda guild_id: self.guild if guild_id == 1 else None,
            wait_until_ready=AsyncMock(),
        )
        self.cog = digest_cog.ModDigestCog(self.bot)

    async def asyncTearDown(self):
        self.cog.cog_unload()

    async def test_skips_on_a_non_monday(self):
        with patch("bulmaai.services.mod_digest.collect", AsyncMock()) as collect:
            await self.cog._tick(TUESDAY)
        collect.assert_not_awaited()

    async def test_skips_when_digest_disabled(self):
        self.bot.settings = fake_settings(moderation_digest_enabled=False)
        with patch("bulmaai.services.mod_digest.collect", AsyncMock()) as collect:
            await self.cog._tick(NOW)
        collect.assert_not_awaited()

    async def test_sends_on_monday_when_enabled(self):
        fake_channel = SimpleNamespace(send=AsyncMock())
        with (
            patch("bulmaai.cogs.mod_digest.resolve_digest_channel", AsyncMock(return_value=fake_channel)),
            patch("bulmaai.services.mod_digest.collect", AsyncMock(return_value="DATA")) as collect,
            patch("bulmaai.services.mod_digest.build_view", return_value="VIEW") as build,
        ):
            await self.cog._tick(NOW)
        collect.assert_awaited_once_with(1, now=NOW, settings=self.bot.settings)
        build.assert_called_once_with("DATA", self.bot.settings)
        fake_channel.send.assert_awaited_once()
        kwargs = fake_channel.send.await_args.kwargs
        self.assertEqual(kwargs["view"], "VIEW")
        self.assertIsInstance(kwargs["allowed_mentions"], discord.AllowedMentions)

    async def test_missing_channel_logs_a_warning_and_skips(self):
        with (
            patch("bulmaai.cogs.mod_digest.resolve_digest_channel", AsyncMock(return_value=None)),
            patch("bulmaai.services.mod_digest.collect", AsyncMock()) as collect,
            self.assertLogs("bulmaai.cogs.mod_digest", "WARNING"),
        ):
            await self.cog._tick(NOW)
        collect.assert_not_awaited()



class DigestViewTests(unittest.IsolatedAsyncioTestCase):  # py-cord views need a running loop
    async def test_view_renders_each_card_within_the_text_limit(self):
        cards = [
            mod_digest.DigestCard("Weekly Moderation Digest", discord.Color.blurple(), [("Reports", "3")], note="Oct 1 – Oct 8"),
            mod_digest.DigestCard("Automod & Filters", discord.Color.dark_gold(), [("Tuning suggestions", "x" * 5000)]),
        ]
        with patch("bulmaai.services.mod_digest.build_cards", return_value=cards):
            view = mod_digest.build_view("DATA", None)
        first, second = view.children
        self.assertEqual(first.items[0].content, "## Weekly Moderation Digest\n-# Oct 1 – Oct 8")
        self.assertEqual(first.items[2].content, "**Reports**\n3")
        self.assertLessEqual(sum(len(item.content) for c in view.children for item in c.items if hasattr(item, "content")), 4000)


if __name__ == "__main__":
    unittest.main()
