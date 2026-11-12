import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import ai_tickets
from bulmaai.cogs import staff_assistant as cog_module

GUILD_ID = 1
MEMBER_ROLE_ID = 7
TICKET_CATEGORY = 500
BOT_ID = 99
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


def make_cog():
    bot = MagicMock()
    bot.settings = SimpleNamespace(
        staff_ai_enabled=True,
        panel_guild_id=GUILD_ID,
        member_role_id=MEMBER_ROLE_ID,
        ai_ticket_category_id=TICKET_CATEGORY,
        ticket_closed_category_id=501,
        ai_support_enabled=True,
    )
    bot.user = SimpleNamespace(id=BOT_ID, mention=f"<@{BOT_ID}>")
    role = object()
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_role.return_value = role
    bot.get_guild.return_value = guild
    cog = cog_module.StaffAssistantCog(bot)
    cog._onboarded = {42}
    return cog, guild, role


def text_channel(role, *, visible=True, category_id=10):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 1000 + category_id
    channel.category_id = category_id
    channel.category = SimpleNamespace(id=category_id)
    channel.permissions_for.side_effect = lambda r: SimpleNamespace(view_channel=visible if r is role else False)
    return channel


def make_message(guild, channel, *, author_id=42, content="hello team", bot=False, mentions=()):
    author = MagicMock()
    author.id = author_id
    author.bot = bot
    return SimpleNamespace(
        id=555, author=author, guild=guild, channel=channel, content=content, webhook_id=None,
        created_at=NOW, mentions=list(mentions), reference=None, attachments=[], components=[],
    )


def dm_message(content="what is the time", author_id=42):
    channel = MagicMock(spec=discord.DMChannel)
    return make_message(None, channel, author_id=author_id, content=content)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, message, cog):
        with patch.object(cog_module.staff_store, "store_message", AsyncMock()) as store:
            await cog._capture(message)
        return store

    async def test_member_visible_channel_is_stored(self) -> None:
        cog, guild, role = make_cog()
        message = make_message(guild, text_channel(role))
        store = await self.capture(message, cog)
        store.assert_awaited_once()
        self.assertEqual(store.await_args.args[:3], (555, 42, GUILD_ID))

    async def test_skipped_cases(self) -> None:
        cog, guild, role = make_cog()
        cases = {
            "staff only": make_message(guild, text_channel(role, visible=False)),
            "ticket": make_message(guild, text_channel(role, category_id=TICKET_CATEGORY)),
            "dm": dm_message(),
            "bot": make_message(guild, text_channel(role), bot=True),
            "not onboarded": make_message(guild, text_channel(role), author_id=43),
            "empty": make_message(guild, text_channel(role), content="  "),
        }
        other_guild = MagicMock()
        other_guild.id = 2
        cases["other guild"] = make_message(other_guild, text_channel(role))
        private = MagicMock(spec=discord.Thread)
        private.type = discord.ChannelType.private_thread
        cases["private thread"] = make_message(guild, private)
        for name, message in cases.items():
            with self.subTest(name):
                store = await self.capture(message, cog)
                store.assert_not_awaited()

    async def test_public_thread_follows_parent(self) -> None:
        cog, guild, role = make_cog()
        thread = MagicMock(spec=discord.Thread)
        thread.type = discord.ChannelType.public_thread
        thread.id = 77
        thread.parent = text_channel(role)
        store = await self.capture(make_message(guild, thread), cog)
        store.assert_awaited_once()
        thread.parent = text_channel(role, visible=False)
        store = await self.capture(make_message(guild, thread), cog)
        store.assert_not_awaited()

    async def test_no_member_role_stores_nothing(self) -> None:
        cog, guild, role = make_cog()
        guild.get_role.return_value = None
        store = await self.capture(make_message(guild, text_channel(role)), cog)
        store.assert_not_awaited()


class EditDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_and_delete(self) -> None:
        cog, _, _ = make_cog()
        with patch.object(cog_module.staff_store, "edit_message", AsyncMock()) as edit, patch.object(
            cog_module.staff_store, "delete_message", AsyncMock()
        ) as delete:
            await cog.on_raw_message_edit(SimpleNamespace(message_id=5, cached_message=None, data={"content": "new"}))
            edit.assert_awaited_once()
            self.assertEqual(edit.await_args.args[:2], (5, "new"))
            other = SimpleNamespace(author=SimpleNamespace(id=43))
            await cog.on_raw_message_edit(SimpleNamespace(message_id=6, cached_message=other, data={"content": "x"}))
            edit.assert_awaited_once()
            await cog.on_raw_message_delete(SimpleNamespace(message_id=5, cached_message=None))
            await cog.on_raw_message_delete(SimpleNamespace(message_id=6, cached_message=other))
            delete.assert_awaited_once_with(5)
            delete.reset_mock()
            mine = SimpleNamespace(id=8, author=SimpleNamespace(id=42))
            await cog.on_raw_bulk_message_delete(
                SimpleNamespace(message_ids={8, 9}, cached_messages=[mine, SimpleNamespace(id=9, author=SimpleNamespace(id=43))])
            )
            delete.assert_awaited_once_with(8)


class FakeHistory:
    def __init__(self, messages):
        self.messages = messages

    def __call__(self, limit=None, **_):
        async def gen():
            for message in self.messages[:limit]:
                yield message

        return gen()


class LearnTests(unittest.IsolatedAsyncioTestCase):
    def setup_learn(self, per_channel=60, channels=3):
        cog, guild, role = make_cog()
        sources = []
        for i in range(channels):
            channel = text_channel(role, category_id=10 + i)
            channel.last_message_id = i
            channel.history = FakeHistory(
                [SimpleNamespace(id=i * 1000 + n, author=SimpleNamespace(id=42), content=f"m{n}", created_at=NOW) for n in range(per_channel)]
            )
            sources.append(channel)
        guild.text_channels = sources
        guild.threads = []
        return cog, guild

    async def test_stops_at_100(self) -> None:
        cog, _ = self.setup_learn()
        with patch.object(cog_module.asyncio, "sleep", AsyncMock()), patch.object(
            cog_module.staff_store, "get_profile", AsyncMock(return_value={"user_id": 42})
        ), patch.object(cog_module.staff_store, "store_message", AsyncMock()) as store, patch.object(
            cog_module.staff_store, "mark_learned", AsyncMock()
        ) as learned, patch.object(cog_module.staff_assistant, "distill", AsyncMock()) as distill:
            await cog._learn(SimpleNamespace(id=42))
        self.assertEqual(store.await_count, 100)
        learned.assert_awaited_once_with(42)
        distill.assert_awaited_once()

    async def test_forgotten_midway_stores_nothing_more(self) -> None:
        cog, _ = self.setup_learn()
        with patch.object(cog_module.asyncio, "sleep", AsyncMock()), patch.object(
            cog_module.staff_store, "get_profile", AsyncMock(return_value=None)
        ), patch.object(cog_module.staff_store, "store_message", AsyncMock()) as store, patch.object(
            cog_module.staff_store, "mark_learned", AsyncMock()
        ) as learned, patch.object(cog_module.staff_assistant, "distill", AsyncMock()) as distill:
            await cog._learn(SimpleNamespace(id=42))
        store.assert_not_awaited()
        learned.assert_not_awaited()
        distill.assert_not_awaited()

    async def test_forbidden_channel_is_skipped(self) -> None:
        cog, guild = self.setup_learn(per_channel=5, channels=2)

        def boom(limit=None, **_):
            async def gen():
                raise discord.Forbidden(MagicMock(status=403), "no")
                yield

            return gen()

        guild.text_channels[0].history = boom
        with patch.object(cog_module.asyncio, "sleep", AsyncMock()), patch.object(
            cog_module.staff_store, "get_profile", AsyncMock(return_value={"user_id": 42})
        ), patch.object(cog_module.staff_store, "store_message", AsyncMock()) as store, patch.object(
            cog_module.staff_store, "mark_learned", AsyncMock()
        ), patch.object(cog_module.staff_assistant, "distill", AsyncMock()):
            await cog._learn(SimpleNamespace(id=42))
        self.assertEqual(store.await_count, 5)


class HandlesTests(unittest.IsolatedAsyncioTestCase):
    async def handles(self, cog, message, *, profile=True, tier=cog_module.Tier.HELPER):
        member = MagicMock(spec=discord.Member)
        member.guild = SimpleNamespace(id=GUILD_ID)
        message.author.guild = member.guild
        with patch.object(cog_module.staff_store, "get_profile", AsyncMock(return_value={"x": 1} if profile else None)), patch.object(
            cog_module, "tier_for", return_value=tier
        ), patch.object(cog, "_gate", AsyncMock(return_value=member)):
            return await cog.handles(message)

    def pinged(self, cog, message):
        message.mentions = [cog.bot.user]
        message.content = f"<@{BOT_ID}> do the thing"
        return message

    async def test_dm(self) -> None:
        cog, _, _ = make_cog()
        self.assertTrue(await self.handles(cog, dm_message()))
        self.assertFalse(await self.handles(cog, dm_message("!ping")))
        self.assertFalse(await self.handles(cog, dm_message("/x")))
        self.assertFalse(await self.handles(cog, dm_message(author_id=43)))
        self.assertFalse(await self.handles(cog, dm_message(), profile=False))

    async def test_ping_in_staff_only_channel(self) -> None:
        cog, guild, role = make_cog()
        message = self.pinged(cog, make_message(guild, text_channel(role, visible=False)))
        self.assertTrue(await self.handles(cog, message))

    async def test_ping_without_content_or_not_pinged(self) -> None:
        cog, guild, role = make_cog()
        channel = text_channel(role, visible=False)
        self.assertFalse(await self.handles(cog, make_message(guild, channel)))
        message = make_message(guild, channel, content=f"<@{BOT_ID}>", mentions=[cog.bot.user])
        self.assertFalse(await self.handles(cog, message))

    async def test_ping_in_member_visible_channel_and_ticket(self) -> None:
        cog, guild, role = make_cog()
        visible = self.pinged(cog, make_message(guild, text_channel(role, visible=True)))
        self.assertFalse(await self.handles(cog, visible))
        ticket = self.pinged(cog, make_message(guild, text_channel(role, visible=False, category_id=TICKET_CATEGORY)))
        self.assertFalse(await self.handles(cog, ticket))

    async def test_not_onboarded_and_gate_closed(self) -> None:
        cog, guild, role = make_cog()
        message = self.pinged(cog, make_message(guild, text_channel(role, visible=False), author_id=43))
        self.assertFalse(await self.handles(cog, message))
        message = self.pinged(cog, make_message(guild, text_channel(role, visible=False)))
        with patch.object(cog_module.staff_store, "get_profile", AsyncMock(return_value={"x": 1})), patch.object(
            cog, "_gate", AsyncMock(return_value=None)
        ):
            self.assertFalse(await cog.handles(message))


class HookTests(unittest.IsolatedAsyncioTestCase):
    def make_ai_cog(self):
        bot = MagicMock()
        bot.settings = SimpleNamespace(ai_support_enabled=True)
        cog = ai_tickets.AITicketsCog.__new__(ai_tickets.AITicketsCog)
        cog.bot = bot
        cog._can_use_support_from_message = AsyncMock(return_value=True)
        cog._schedule_support_response = MagicMock()
        return cog

    async def test_dm_steps_aside_when_staff_handles(self) -> None:
        cog = self.make_ai_cog()
        staff = SimpleNamespace(handles=AsyncMock(return_value=True))
        cog.bot.get_cog.return_value = staff
        await cog.on_message(dm_message())
        cog._schedule_support_response.assert_not_called()

    async def test_dm_unchanged_otherwise(self) -> None:
        cog = self.make_ai_cog()
        cog.bot.get_cog.return_value = SimpleNamespace(handles=AsyncMock(return_value=False))
        await cog.on_message(dm_message())
        cog._schedule_support_response.assert_called_once()
        cog.bot.get_cog.return_value = None
        cog._schedule_support_response.reset_mock()
        await cog.on_message(dm_message())
        cog._schedule_support_response.assert_called_once()


if __name__ == "__main__":
    unittest.main()
