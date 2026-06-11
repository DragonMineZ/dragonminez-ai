import unittest
from types import SimpleNamespace

import discord

from bulmaai.cogs.patreon_announcements import (
    _build_dm_welcome_embed,
    _downloads_channel_url,
)
from bulmaai.cogs.patreon_whitelist_flow import PatreonWhitelistFlowCog
from bulmaai.ui.patreon_views import (
    PATREON_WELCOME_VERIFY_CUSTOM_ID,
    PatreonWelcomeView,
)


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


class PatreonWelcomeViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_has_verify_button_and_downloads_link(self) -> None:
        view = PatreonWelcomeView(downloads_channel_url="https://discord.com/channels/999/123")

        custom_ids = [getattr(child, "custom_id", None) for child in view.children]
        urls = [getattr(child, "url", None) for child in view.children]
        self.assertIn(PATREON_WELCOME_VERIFY_CUSTOM_ID, custom_ids)
        self.assertIn("https://discord.com/channels/999/123", urls)
        self.assertIsNone(view.timeout)

    async def test_view_without_downloads_url_only_has_verify_button(self) -> None:
        view = PatreonWelcomeView(downloads_channel_url=None)

        self.assertEqual(len(view.children), 1)


class PatreonWelcomeDmTests(unittest.TestCase):
    def test_dm_embed_contains_quick_start_steps(self) -> None:
        embed = _build_dm_welcome_embed(member=_fake_member(), role=_fake_role())

        field_names = " ".join(field.name for field in embed.fields)
        field_values = " ".join(field.value for field in embed.fields)
        self.assertIn("1. Verify your access", field_names)
        self.assertIn("2. Get whitelisted", field_names)
        self.assertIn("3. Download and play", field_names)
        self.assertIn("Verify & Get Beta Access", field_values)
        self.assertIn("/beta-access", field_values)
        self.assertIn("one-time per user per build", field_values)
        self.assertIn("Supporter perk does NOT include", field_values)

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
