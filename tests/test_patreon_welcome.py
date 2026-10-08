import unittest
from types import SimpleNamespace

import discord

from bulmaai.cogs.patreon_announcements import (
    _build_dm_welcome_card,
    _downloads_channel_url,
)
from bulmaai.cogs.patreon_whitelist_flow import PatreonWhitelistFlowCog
from bulmaai.ui.patreon_views import (
    PATREON_WELCOME_VERIFY_CUSTOM_ID,
    welcome_buttons,
)
from v2_helpers import buttons, text


def _fake_member() -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        mention="<@42>",
        display_name="Bruno",
        display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
        guild=SimpleNamespace(id=999, name="DragonMineZ"),
    )


def _fake_role() -> SimpleNamespace:
    return SimpleNamespace(id=7, mention="<@&7>", name="Contributor")


class PatreonWelcomeButtonsTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_has_verify_button_and_downloads_link(self) -> None:
        buttons = welcome_buttons(downloads_channel_url="https://discord.com/channels/999/123")

        custom_ids = [getattr(button, "custom_id", None) for button in buttons]
        urls = [getattr(button, "url", None) for button in buttons]
        self.assertIn(PATREON_WELCOME_VERIFY_CUSTOM_ID, custom_ids)
        self.assertIn("https://discord.com/channels/999/123", urls)

    async def test_view_without_downloads_url_only_has_verify_button(self) -> None:
        self.assertEqual(len(welcome_buttons(downloads_channel_url=None)), 1)


class PatreonWelcomeDmTests(unittest.IsolatedAsyncioTestCase):  # py-cord views need a running loop
    async def test_dm_card_contains_quick_start_steps_and_buttons(self) -> None:
        view = _build_dm_welcome_card(
            member=_fake_member(), role=_fake_role(), buttons=welcome_buttons(downloads_channel_url=None)
        )

        card_text = text(view)
        self.assertIn("**1. Verify your access**", card_text)
        self.assertIn("**2. Get whitelisted**", card_text)
        self.assertIn("**3. Download and play**", card_text)
        self.assertIn("Verify & Get Beta Access", card_text)
        self.assertIn("/patreon beta-access", card_text)
        self.assertIn("one-time per user per build", card_text)
        self.assertIn("Supporter perk does NOT include", card_text)
        self.assertEqual([b.label for b in buttons(view)], ["Verify & Get Beta Access"])

    def test_downloads_channel_url_built_from_member_guild(self) -> None:
        self.assertEqual(
            _downloads_channel_url(_fake_member(), 123),
            "https://discord.com/channels/999/123",
        )
        self.assertIsNone(_downloads_channel_url(_fake_member(), None))


class PatreonWelcomeVerifyButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_button_opens_username_modal(self) -> None:
        sent_modals: list[object] = []

        async def send_modal(modal) -> None:
            sent_modals.append(modal)

        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        interaction = SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": PATREON_WELCOME_VERIFY_CUSTOM_ID},
            response=SimpleNamespace(send_modal=send_modal),
        )

        await cog.on_interaction(interaction)

        self.assertEqual(len(sent_modals), 1)

    async def test_other_component_interactions_are_ignored(self) -> None:
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        interaction = SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": "something_else"},
            response=SimpleNamespace(),
        )

        await cog.on_interaction(interaction)


if __name__ == "__main__":
    unittest.main()
