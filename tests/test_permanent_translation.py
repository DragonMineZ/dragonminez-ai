"""Translations keep working in a permanent channel (/unticket): on_message hands human messages to
_translate_message when the channel is marked permanent with translations on, and its last gate allows it."""
from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bulmaai.cogs import ai_tickets
from bulmaai.cogs.ai_tickets import AITicketsCog
from test_staff_copilot import BOT, OWNER, SETTINGS, STAFF, TranslationTests, as_member

PERMANENT_ID = 700
OTHER_CATEGORY = 55  # not the open-tickets category


def permanent_channel(channel_id=PERMANENT_ID):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.category = types.SimpleNamespace(id=OTHER_CATEGORY)
    channel.send = AsyncMock()
    return channel


def human(user):
    author = as_member(user)
    author.bot = False
    return author


def posted(channel, author):
    return types.SimpleNamespace(
        id=5, author=author, channel=channel, guild=object(), content="Send your latest log please",
        clean_content="Send your latest log please", attachments=[], snapshots=[], mentions=[], reference=None,
    )


def make_cog():
    return AITicketsCog(types.SimpleNamespace(settings=SETTINGS, user=BOT, get_cog=lambda name: None, guilds=[]))


class OnMessageTests(unittest.IsolatedAsyncioTestCase):
    async def run_message(self, cog, message):
        with (
            patch.object(cog, "_schedule_translation") as schedule,
            patch.object(cog, "_schedule_support_response") as answer,
            patch.object(cog, "_help_thread_requester", AsyncMock(return_value=None)),
            patch.object(cog, "_can_use_support_from_message", AsyncMock(return_value=True)),
        ):
            await cog.on_message(message)
        answer.assert_not_called()  # a permanent channel is not a ticket: no AI answer
        return schedule

    async def test_a_human_in_a_permanent_channel_with_translations_on_is_translated(self):
        cog = make_cog()
        cog.set_permanent_channel(PERMANENT_ID, True)
        schedule = await self.run_message(cog, posted(permanent_channel(), human(STAFF)))
        schedule.assert_called_once()

    async def test_translations_off_an_ordinary_channel_and_bots_are_not(self):
        cog = make_cog()
        cog.set_permanent_channel(PERMANENT_ID, False)
        schedule = await self.run_message(cog, posted(permanent_channel(), human(STAFF)))
        schedule.assert_not_called()  # switched off
        cog.set_permanent_channel(PERMANENT_ID, True)
        schedule = await self.run_message(cog, posted(permanent_channel(701), human(STAFF)))
        schedule.assert_not_called()  # not a permanent channel
        webhook_post = posted(permanent_channel(), types.SimpleNamespace(id=777, bot=True, name="V", display_name="V"))
        webhook_post.webhook_id = 777  # our own translation coming back
        schedule = await self.run_message(cog, webhook_post)
        schedule.assert_not_called()
        bot_post = posted(permanent_channel(), as_member(STAFF))
        bot_post.author.bot = True
        schedule = await self.run_message(cog, bot_post)
        schedule.assert_not_called()

    async def test_set_permanent_channel_can_switch_it_back_on(self):
        cog = make_cog()
        cog.set_permanent_channel(PERMANENT_ID, True)
        cog.set_permanent_channel(PERMANENT_ID, False)
        self.assertEqual(cog._permanent_channels, {PERMANENT_ID: False})

    async def test_startup_loads_the_permanent_channels(self):
        cog = make_cog()
        with (
            patch.object(ai_tickets, "get_ai_disabled_ticket_channels", AsyncMock(return_value=set())),
            patch.object(ai_tickets, "list_permanent_translation", AsyncMock(return_value={PERMANENT_ID: True, 8: False})),
        ):
            await cog.on_startup()
        self.assertEqual(cog._permanent_channels, {PERMANENT_ID: True, 8: False})


class LookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_language_and_owner_lookups_also_find_permanent_rows(self):
        cog = make_cog()
        ticket = types.SimpleNamespace(language="pt", category="contribute", owner_id=OWNER.id)
        with patch.object(ai_tickets, "get_ticket_by_channel", AsyncMock(return_value=ticket)) as lookup:
            self.assertEqual(await cog._ticket_language(PERMANENT_ID), "pt")
            self.assertEqual(lookup.await_args.kwargs, {"permanent": True})
            channel = permanent_channel()
            self.assertTrue(await cog._is_ticket_owner(channel, OWNER.id))
            self.assertEqual(lookup.await_args.kwargs, {"permanent": True})


class PermanentTranslateTests(TranslationTests):
    """Reuses the ticket translation harness with a channel outside the tickets category."""

    def message(self, *args, **kwargs):
        message = super().message(*args, **kwargs)
        message.channel.category = types.SimpleNamespace(id=OTHER_CATEGORY)
        return message

    async def test_it_translates_in_a_permanent_channel_with_translations_on(self):
        cog = self.cog()
        cog.set_permanent_channel(900, True)
        message = self.message()
        await self.translate(cog, message)
        cog._translate_webhooks[900].send.assert_awaited_once()

    async def test_it_does_not_when_the_channel_is_neither_ticket_nor_permanent(self):
        for permanent in (None, False):
            cog = self.cog()
            if permanent is not None:
                cog.set_permanent_channel(900, permanent)
            message = self.message()
            await self.translate(cog, message)
            self.assertNotIn(900, cog._translate_webhooks)
            message.channel.send.assert_not_called()


# Only this file's own cases: the inherited ticket tests already run in test_staff_copilot.
for _name in [n for n in dir(TranslationTests) if n.startswith("test_")]:
    if _name not in PermanentTranslateTests.__dict__:
        setattr(PermanentTranslateTests, _name, None)

del TranslationTests


if __name__ == "__main__":
    unittest.main()
