import asyncio
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from PIL import Image

from bulmaai.cogs.welcome import AUTO_JOIN_REASON, WelcomeCog, build_welcome_message, resolve_member_role
from bulmaai.services.welcome_card import CANVAS_HEIGHT, CANVAS_WIDTH, render_welcome_card


def make_settings(**overrides):
    base = dict(panel_guild_id=1, welcome_channel_id=50, member_role_id=77, member_role_name="Member")
    base.update(overrides)
    return SimpleNamespace(**base)


def make_avatar_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 90)).save(output, format="PNG")
    return output.getvalue()


def make_member(*, guild, roles=(), bot=False, pending=False):
    avatar = MagicMock()
    avatar.with_size.return_value.with_static_format.return_value.read = AsyncMock(return_value=make_avatar_bytes())
    return SimpleNamespace(
        id=42,
        name="zoned.out",
        bot=bot,
        pending=pending,
        guild=guild,
        roles=list(roles),
        mention="<@42>",
        display_avatar=avatar,
        add_roles=AsyncMock(),
    )


def make_guild(*, role=None, channel=None, guild_id=1):
    guild = MagicMock()
    guild.id = guild_id
    guild.roles = [role] if role else []
    guild.get_role.side_effect = lambda role_id: role if role and role.id == role_id else None
    guild.get_channel.side_effect = lambda channel_id: channel if channel and channel.id == channel_id else None
    return guild


def make_cog(settings) -> WelcomeCog:
    return WelcomeCog(SimpleNamespace(settings=settings))


class WelcomeCardTests(unittest.TestCase):
    def test_renders_transparent_png_at_canvas_size(self):
        card = render_welcome_card(make_avatar_bytes(), "zoned.out")
        image = Image.open(io.BytesIO(card))
        self.assertEqual(image.size, (CANVAS_WIDTH, CANVAS_HEIGHT))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_long_username_still_renders(self):
        card = render_welcome_card(make_avatar_bytes(), "a" * 32)
        self.assertEqual(Image.open(io.BytesIO(card)).size, (CANVAS_WIDTH, CANVAS_HEIGHT))


class WelcomeMessageTests(unittest.TestCase):
    def test_message_matches_template(self):
        member = SimpleNamespace(mention="<@42>")
        self.assertEqual(
            build_welcome_message(member),
            "Welcome to the DragonMine Z ✨ community, <@42>‼️\nHope you enjoy! 💕",
        )


class ResolveRoleTests(unittest.TestCase):
    def test_prefers_configured_id(self):
        role = SimpleNamespace(id=77, name="Other")
        guild = make_guild(role=role)
        self.assertIs(resolve_member_role(guild, make_settings()), role)

    def test_falls_back_to_name_when_no_id(self):
        role = SimpleNamespace(id=5, name="Member")
        guild = make_guild(role=role)
        self.assertIs(resolve_member_role(guild, make_settings(member_role_id=None)), role)

    def test_missing_role_returns_none(self):
        guild = make_guild()
        self.assertIsNone(resolve_member_role(guild, make_settings()))


class WelcomeCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_adds_role_with_auto_join_reason_and_sends_welcome(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        member = make_member(guild=make_guild(role=role, channel=channel))

        await make_cog(make_settings()).on_member_join(member)

        member.add_roles.assert_awaited_once_with(role, reason=AUTO_JOIN_REASON)
        channel.send.assert_awaited_once()
        args, kwargs = channel.send.await_args
        self.assertEqual(args[0], build_welcome_message(member))
        self.assertIsInstance(kwargs["file"], discord.File)
        self.assertEqual(kwargs["allowed_mentions"].users, [member])

    async def test_pending_join_waits_for_terms_acceptance(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        member = make_member(guild=make_guild(role=role, channel=channel), pending=True)

        await make_cog(make_settings()).on_member_join(member)

        member.add_roles.assert_not_awaited()
        channel.send.assert_not_awaited()

    async def test_accepting_terms_grants_role_and_welcomes(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        guild = make_guild(role=role, channel=channel)
        before = make_member(guild=guild, pending=True)
        after = make_member(guild=guild, pending=False)

        await make_cog(make_settings()).on_member_update(before, after)

        after.add_roles.assert_awaited_once_with(role, reason=AUTO_JOIN_REASON)
        channel.send.assert_awaited_once()

    async def test_unrelated_member_update_does_nothing(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        guild = make_guild(role=role, channel=channel)
        before = make_member(guild=guild)
        after = make_member(guild=guild)

        await make_cog(make_settings()).on_member_update(before, after)

        after.add_roles.assert_not_awaited()
        channel.send.assert_not_awaited()

    async def test_ignores_bots(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        member = make_member(guild=make_guild(role=role, channel=channel), bot=True)

        await make_cog(make_settings()).on_member_join(member)

        member.add_roles.assert_not_awaited()
        channel.send.assert_not_awaited()

    async def test_ignores_other_guilds(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        member = make_member(guild=make_guild(role=role, channel=channel, guild_id=2))

        await make_cog(make_settings()).on_member_join(member)

        member.add_roles.assert_not_awaited()
        channel.send.assert_not_awaited()

    async def test_skips_role_the_member_already_has(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        member = make_member(guild=make_guild(role=role, channel=channel), roles=[role])

        await make_cog(make_settings()).on_member_join(member)

        member.add_roles.assert_not_awaited()
        channel.send.assert_awaited_once()

    async def test_role_failure_does_not_block_welcome(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        member = make_member(guild=make_guild(role=role, channel=channel))
        member.add_roles.side_effect = discord.HTTPException(MagicMock(status=403), "denied")

        await make_cog(make_settings()).on_member_join(member)

        channel.send.assert_awaited_once()

    async def test_unconfigured_channel_still_grants_role(self):
        role = SimpleNamespace(id=77, name="Member")
        member = make_member(guild=make_guild(role=role))

        await make_cog(make_settings(welcome_channel_id=None)).on_member_join(member)

        member.add_roles.assert_awaited_once_with(role, reason=AUTO_JOIN_REASON)

    async def test_avatar_failure_sends_text_only(self):
        role = SimpleNamespace(id=77, name="Member")
        channel = SimpleNamespace(id=50, send=AsyncMock())
        member = make_member(guild=make_guild(role=role, channel=channel))
        member.display_avatar.with_size.return_value.with_static_format.return_value.read = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=404), "gone")
        )

        await make_cog(make_settings()).on_member_join(member)

        self.assertIsNone(channel.send.await_args.kwargs["file"])


if __name__ == "__main__":
    unittest.main()
