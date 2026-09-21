import logging
import time

import discord
from discord.ext import commands

from bulmaai.config import Settings
from bulmaai.services.member_activity import (
    award_xp,
    get_leaderboard,
    get_member_activity,
    level_for_xp,
    parse_role_reward_map,
    set_level,
)
from bulmaai.ui.power_level_views import build_leaderboard_embed, build_power_level_embed


log = logging.getLogger(__name__)

XP_PER_MESSAGE = 15
MESSAGE_COOLDOWN_SECONDS = 60.0
MAX_LEADERBOARD_SIZE = 25


class PowerLevelCog(commands.Cog):
    """DragonMineZ-themed, message-based leveling."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        # (guild_id, user_id) -> last award time (monotonic); a single-shot
        # cooldown, checked before any DB write.
        self._last_award: dict[tuple[int, int], float] = {}

    def _settings(self) -> Settings:
        return self.bot.settings

    def _on_cooldown(self, guild_id: int, user_id: int, *, now: float) -> bool:
        key = (guild_id, user_id)
        last = self._last_award.get(key)
        if last is not None and now - last < MESSAGE_COOLDOWN_SECONDS:
            return True
        self._last_award[key] = now
        return False

    async def _grant_role_rewards(
        self,
        member: discord.Member,
        *,
        previous_level: int,
        new_level: int,
    ) -> None:
        role_map = parse_role_reward_map(self._settings().power_level_role_rewards)
        if not role_map:
            return
        guild = member.guild
        for level in range(previous_level + 1, new_level + 1):
            role_id = role_map.get(level)
            if role_id is None:
                continue
            role = guild.get_role(role_id)
            if role is None:
                continue
            try:
                await member.add_roles(role, reason=f"Reached power level {level}")
            except discord.Forbidden:
                log.warning(
                    "Missing permission to grant power level role",
                    extra={
                        "event": "power_level_role_grant_forbidden",
                        "guild_id": guild.id,
                        "user_id": member.id,
                        "role_id": role_id,
                    },
                )
            except discord.HTTPException:
                log.exception(
                    "Failed to grant power level role",
                    extra={
                        "event": "power_level_role_grant_failed",
                        "guild_id": guild.id,
                        "user_id": member.id,
                        "role_id": role_id,
                    },
                )

    async def _award_message_xp(self, message: discord.Message) -> None:
        settings = self._settings()
        if not settings.power_level_enabled or message.author.bot:
            return
        if not message.guild or not isinstance(message.author, discord.Member):
            return
        if message.channel.id in set(settings.power_level_excluded_channel_ids):
            return
        if self._on_cooldown(message.guild.id, message.author.id, now=time.monotonic()):
            return

        new_xp = await award_xp(message.guild.id, message.author.id, XP_PER_MESSAGE)
        previous_level = level_for_xp(new_xp - XP_PER_MESSAGE)
        new_level = level_for_xp(new_xp)
        if new_level == previous_level:
            return

        await set_level(message.guild.id, message.author.id, new_level)
        if new_level > previous_level:
            await self._grant_role_rewards(
                message.author,
                previous_level=previous_level,
                new_level=new_level,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._award_message_xp(message)

    @discord.slash_command(name="powerlevel", description="Check your (or another member's) power level.")
    @discord.option(
        "member",
        discord.Member,
        description="Member to check (defaults to you)",
        required=False,
        default=None,
    )
    async def powerlevel(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Member | None = None,
    ) -> None:
        if ctx.guild is None:
            return await ctx.respond("This command only works in a server.", ephemeral=True)

        target = member or ctx.author
        activity = await get_member_activity(ctx.guild.id, target.id)
        embed = build_power_level_embed(
            display_name=target.display_name,
            avatar_url=target.display_avatar.url if target.display_avatar else None,
            xp=activity.xp,
            level=activity.level,
        )
        await ctx.respond(embed=embed)

    @discord.slash_command(name="leaderboard", description="Show the top power levels in this server.")
    @discord.option(
        "top",
        int,
        description="Number of members to show (default from settings, max 25)",
        required=False,
        default=None,
    )
    async def leaderboard(
        self,
        ctx: discord.ApplicationContext,
        top: int | None = None,
    ) -> None:
        if ctx.guild is None:
            return await ctx.respond("This command only works in a server.", ephemeral=True)

        default_size = self._settings().power_level_leaderboard_size
        limit = min(max(top or default_size, 1), MAX_LEADERBOARD_SIZE)
        rows = await get_leaderboard(ctx.guild.id, limit)

        entries: list[tuple[str, int, int]] = []
        for row in rows:
            resolved_member = ctx.guild.get_member(row.user_id)
            display_name = resolved_member.display_name if resolved_member else f"User {row.user_id}"
            entries.append((display_name, row.xp, row.level))

        embed = build_leaderboard_embed(guild_name=ctx.guild.name, entries=entries)
        await ctx.respond(embed=embed)


def setup(bot: discord.Bot):
    bot.add_cog(PowerLevelCog(bot))
