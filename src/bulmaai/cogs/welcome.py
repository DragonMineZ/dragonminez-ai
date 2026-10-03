import asyncio
import io
import logging

import discord
from discord.ext import commands

from bulmaai.services.welcome_card import render_welcome_card

log = logging.getLogger(__name__)

AUTO_JOIN_REASON = "Auto-join"
WELCOME_MESSAGE = (
    "Welcome to the DragonMine Z ✨ community, {mention}‼️\n"
    "Hope you enjoy! 💕"
)


def resolve_member_role(guild: discord.Guild, settings) -> discord.Role | None:
    if settings.member_role_id:
        return guild.get_role(settings.member_role_id)
    return discord.utils.get(guild.roles, name=settings.member_role_name)


def build_welcome_message(member: discord.Member) -> str:
    return WELCOME_MESSAGE.format(mention=member.mention)


class WelcomeCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    def _eligible(self, member: discord.Member) -> bool:
        return not member.bot and member.guild.id == self.bot.settings.panel_guild_id

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if self._eligible(member) and not member.pending:
            await self._onboard(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if self._eligible(after) and before.pending and not after.pending:
            await self._onboard(after)

    async def _onboard(self, member: discord.Member) -> None:
        await self._grant_member_role(member)
        await self._send_welcome(member)

    async def _grant_member_role(self, member: discord.Member) -> None:
        role = resolve_member_role(member.guild, self.bot.settings)
        if role is None:
            log.warning(
                "Member role not found; skipping auto-join role",
                extra={"event": "welcome_role_missing", "guild_id": member.guild.id},
            )
            return
        if role in member.roles:
            return
        try:
            await member.add_roles(role, reason=AUTO_JOIN_REASON)
        except discord.HTTPException:
            log.exception(
                "Failed to add the Member role on join",
                extra={"event": "welcome_role_failed", "user_id": member.id, "role_id": role.id},
            )

    async def _send_welcome(self, member: discord.Member) -> None:
        channel_id = self.bot.settings.welcome_channel_id
        if not channel_id:
            return
        channel = member.guild.get_channel(channel_id)
        if channel is None:
            log.warning(
                "Welcome channel not found",
                extra={"event": "welcome_channel_missing", "channel_id": channel_id},
            )
            return

        file = await self._build_card(member)
        try:
            await channel.send(
                build_welcome_message(member),
                file=file,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
        except discord.HTTPException:
            log.exception(
                "Failed to send the welcome message",
                extra={"event": "welcome_send_failed", "user_id": member.id},
            )

    async def _build_card(self, member: discord.Member) -> discord.File | None:
        try:
            avatar_bytes = await member.display_avatar.with_size(256).with_static_format("png").read()
            card = await asyncio.to_thread(render_welcome_card, avatar_bytes, member.name)
        except Exception:
            log.exception(
                "Failed to render the welcome card",
                extra={"event": "welcome_card_failed", "user_id": member.id},
            )
            return None
        return discord.File(io.BytesIO(card), filename=f"welcome_{member.id}.png")


def setup(bot: discord.Bot):
    bot.add_cog(WelcomeCog(bot))
