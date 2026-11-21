"""The "Translate" message command: an ephemeral AI translation with 🇬🇧/🇪🇸/🇧🇷 buttons."""

import os
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import translate as tr
from v2_helpers import buttons, sent_message, texts

GUILD = 1
ENGLISH = "Players who believe they have been unfairly banned can submit an appeal through our website."
SETTINGS = types.SimpleNamespace(
    ai_translation_model="claude-haiku-5-5",
    ai_support_timeout_seconds=30,
    moderation_banned_words=(),
    moderation_blocked_domains=(),
    wiki_base_url="https://wiki.dragonminez.com",
    panel_guild_id=GUILD,
    dev_guild_id=None,
)


def fake_client(text: str):
    response = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock(return_value=response)))


def source(text: str = ENGLISH, message_id: int = 55):
    return types.SimpleNamespace(id=message_id, clean_content=text, components=[], snapshots=[])


def button_states(view) -> dict[str, bool]:
    return {item.custom_id: item.disabled for item in buttons(view)}


class TranslateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cog = tr.TranslateCog(types.SimpleNamespace(settings=SETTINGS))
        for patcher in (
            patch.object(tr, "reader_language", lambda *_: "es"),
            patch.object(tr.ai_budget, "is_paused", lambda _settings: False),
            patch.object(tr.ai_budget, "record_response"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ctx(self):
        return types.SimpleNamespace(
            interaction=types.SimpleNamespace(guild_id=GUILD, user=types.SimpleNamespace(id=42)),
            respond=AsyncMock(),
            defer=AsyncMock(),
        )

    def test_it_is_a_message_command_for_servers_and_bot_dms(self) -> None:
        command = tr.TranslateCog.translate_message
        self.assertIsInstance(command, discord.MessageCommand)
        self.assertEqual(command.name, "Translate")
        self.assertEqual(command.contexts, {discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm})

    def test_parse_button(self) -> None:
        self.assertEqual(tr.parse_button("translate:55:pt"), (55, "pt"))
        self.assertIsNone(tr.parse_button("translate:55:de"))
        self.assertIsNone(tr.parse_button("translate:abc:en"))

    async def test_translation_is_screened_and_keeps_the_messages_own_links(self) -> None:
        text = f"{ENGLISH} https://appeals.dragonminez.com/"
        client = fake_client("Los jugadores pueden apelar en https://appeals.dragonminez.com/ @everyone")
        translation = await tr.translate(text, "es", SETTINGS, client=client)
        self.assertIn("https://appeals.dragonminez.com/", translation)
        self.assertNotIn("@everyone", translation)
        self.assertIn("Latin American Spanish", client.messages.create.await_args.kwargs["system"])

    async def test_a_blocked_translation_is_dropped(self) -> None:
        self.assertIsNone(await tr.translate(ENGLISH, "es", SETTINGS, client=fake_client("<invoke name='x'>")))

    async def test_opens_ephemerally_in_the_readers_language(self) -> None:
        ctx = self.ctx()
        with patch.object(tr, "translate", AsyncMock(return_value="Los jugadores pueden apelar.")) as translate:
            await tr.TranslateCog.translate_message.callback(self.cog, ctx, source())
        self.assertEqual(translate.await_args.args[1], "es")
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(ctx.respond.await_args.kwargs["ephemeral"])
        view = ctx.respond.await_args.kwargs["view"]
        self.assertIn("Los jugadores pueden apelar.", texts(view))
        self.assertEqual(
            button_states(view), {"translate:55:en": False, "translate:55:es": True, "translate:55:pt": False}
        )

    async def test_a_message_already_in_the_readers_language_only_gets_the_buttons(self) -> None:
        ctx = self.ctx()
        with patch.object(tr, "translate", AsyncMock()) as translate:
            await tr.TranslateCog.translate_message.callback(
                self.cog, ctx, source("¿Cómo instalo el mod? No me funciona y ya probé todo, ¿qué hago?")
            )
        translate.assert_not_awaited()
        view = ctx.respond.await_args.kwargs["view"]
        self.assertIn(tr.TEXT["es"]["pick"], texts(view))
        self.assertFalse(any(button_states(view).values()))

    async def test_empty_and_rate_limited_are_refused_without_a_call(self) -> None:
        ctx = self.ctx()
        with patch.object(tr, "translate", AsyncMock()) as translate:
            await tr.TranslateCog.translate_message.callback(self.cog, ctx, source(""))
            self.assertEqual(ctx.respond.await_args.args[0], tr.TEXT["es"]["empty"])
            self.cog._uses[42] = [tr.time.monotonic()] * tr.RATE_LIMIT_CALLS
            await tr.TranslateCog.translate_message.callback(self.cog, ctx, source())
            self.assertEqual(ctx.respond.await_args.args[0], tr.TEXT["es"]["rate_limited"])
        translate.assert_not_awaited()

    async def test_a_language_button_swaps_the_card(self) -> None:
        interaction = MagicMock()
        interaction.type = discord.InteractionType.component
        interaction.data = {"custom_id": "translate:55:pt"}
        interaction.message = sent_message(tr.translation_card(55, "Los jugadores pueden apelar.", "es", "es"))
        interaction.guild_id = GUILD
        interaction.user.id = 42
        interaction.channel.fetch_message = AsyncMock(return_value=source())
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        with patch.object(tr, "translate", AsyncMock(return_value="Os jogadores podem apelar.")) as translate:
            await self.cog.on_interaction(interaction)
        self.assertEqual(translate.await_args.args[1], "pt")
        interaction.channel.fetch_message.assert_awaited_once_with(55)
        view = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIn("Os jogadores podem apelar.", texts(view))
        self.assertTrue(button_states(view)["translate:55:pt"])


if __name__ == "__main__":
    unittest.main()
