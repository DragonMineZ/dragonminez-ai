"""Weekly moderation digest: posts services.mod_digest's embeds to a staff channel every Monday,
and exposes /digest preview (a dry run only the caller sees) and /digest send (post it now)."""

import logging
from datetime import datetime, time, timezone

import discord
from discord.ext import commands, tasks

from bulmaai.services import mod_actions, mod_digest
from bulmaai.web.core import PERMISSIONS, tier_for

log = logging.getLogger(__name__)

# ponytail: fixed on purpose, not a setting — Monday 14:00 UTC is the moderation team's weekly sync.
DIGEST_WEEKDAY = 0  # Monday
DIGEST_HOUR_UTC = 14
STAFF_GENERAL_CHANNEL_NAME = "staff-general"
STAFF_ONLY = discord.Permissions(moderate_members=True)


async def resolve_digest_channel(bot: discord.Bot, guild: discord.Guild) -> discord.abc.Messageable | None:
    """moderation_digest_channel_id if set, else the text channel named staff-general in this guild."""
    settings = bot.settings
    if settings.moderation_digest_channel_id:
        return await mod_actions.resolve_channel(bot, settings.moderation_digest_channel_id)
    return discord.utils.get(guild.text_channels, name=STAFF_GENERAL_CHANNEL_NAME)


class ModDigestCog(commands.Cog):
    digest_group = discord.SlashCommandGroup(
        "digest", "Weekly moderation digest", default_member_permissions=STAFF_ONLY
    )

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.send_digest.start()

    def cog_unload(self) -> None:
        self.send_digest.cancel()

    # --- the weekly send -----------------------------------------------------------------------

    @tasks.loop(time=time(DIGEST_HOUR_UTC, 0, tzinfo=timezone.utc))
    async def send_digest(self) -> None:
        await self._tick(discord.utils.utcnow())

    @send_digest.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick(self, now: datetime) -> None:
        """The loop body, factored out so tests can drive it with a fixed `now` instead of waiting
        for the schedule. Only ever sends on Mondays, and only when the digest is enabled."""
        settings = self.bot.settings
        if now.weekday() != DIGEST_WEEKDAY or not settings.moderation_digest_enabled:
            return
        guild = self.bot.get_guild(settings.panel_guild_id)
        if guild is None:
            return
        channel = await resolve_digest_channel(self.bot, guild)
        if channel is None:
            log.warning(
                "No moderation digest channel configured or found; skipping this week's digest",
                extra={"event": "mod_digest_channel_missing", "guild_id": guild.id},
            )
            return
        await self._send_to(channel, guild.id, now)

    async def _send_to(self, channel: discord.abc.Messageable, guild_id: int, now: datetime) -> None:
        data = await mod_digest.collect(guild_id, now=now, settings=self.bot.settings)
        embeds = mod_digest.build_embeds(data, self.bot.settings)
        await channel.send(embeds=embeds, allowed_mentions=discord.AllowedMentions.none())

    # --- /digest ---------------------------------------------------------------------------------

    async def _allowed(self, ctx: discord.ApplicationContext, capability: str) -> bool:
        if tier_for(ctx.user, self.bot.settings) >= PERMISSIONS[capability]:
            return True
        await ctx.respond("Your staff tier can't do that.", ephemeral=True)
        return False

    @digest_group.command(name="preview", description="Preview the weekly moderation digest (only visible to you)")
    async def preview(self, ctx: discord.ApplicationContext):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        await ctx.defer(ephemeral=True)
        data = await mod_digest.collect(ctx.guild.id, now=discord.utils.utcnow(), settings=self.bot.settings)
        embeds = mod_digest.build_embeds(data, self.bot.settings)
        await ctx.respond(embeds=embeds, ephemeral=True)

    @digest_group.command(name="send", description="Post the weekly moderation digest to the digest channel now")
    async def send(self, ctx: discord.ApplicationContext):
        if not await self._allowed(ctx, "settings.edit"):
            return
        await ctx.defer(ephemeral=True)
        channel = await resolve_digest_channel(self.bot, ctx.guild)
        if channel is None:
            return await ctx.respond("No digest channel is configured, and there's no #staff-general.", ephemeral=True)
        await self._send_to(channel, ctx.guild.id, discord.utils.utcnow())
        await ctx.respond(f"Sent to {channel.mention}.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(ModDigestCog(bot))
