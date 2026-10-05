"""Weekly moderation digest: posts services.mod_digest's embeds to a staff channel every Monday."""

import logging
from datetime import datetime, time, timezone

import discord
from discord.ext import tasks

from bulmaai.services import mod_actions, mod_digest
from bulmaai.utils.lifecycle import ReloadableCog

log = logging.getLogger(__name__)

# ponytail: fixed on purpose, not a setting — Monday 14:00 UTC is the moderation team's weekly sync.
DIGEST_WEEKDAY = 0  # Monday
DIGEST_HOUR_UTC = 14
STAFF_GENERAL_CHANNEL_NAME = "staff-general"


async def resolve_digest_channel(bot: discord.Bot, guild: discord.Guild) -> discord.abc.Messageable | None:
    """moderation_digest_channel_id if set, else the text channel named staff-general in this guild."""
    settings = bot.settings
    if settings.moderation_digest_channel_id:
        return await mod_actions.resolve_channel(bot, settings.moderation_digest_channel_id)
    return discord.utils.get(guild.text_channels, name=STAFF_GENERAL_CHANNEL_NAME)


class ModDigestCog(ReloadableCog):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    async def on_startup(self) -> None:
        if not self.send_digest.is_running():
            self.send_digest.start()

    async def on_shutdown(self) -> None:
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


def setup(bot: discord.Bot) -> None:
    bot.add_cog(ModDigestCog(bot))
