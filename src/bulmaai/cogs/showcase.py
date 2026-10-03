import logging
from datetime import timedelta

import discord
from discord.ext import commands

from bulmaai.config import Settings
from bulmaai.services import showcase
from bulmaai.services.ai_guard import defang
from bulmaai.ui.showcase_views import build_showcase_highlight_embed

log = logging.getLogger(__name__)

MIN_VOTER_ACCOUNT_AGE = timedelta(days=7)


def _first_image_url(message: discord.Message) -> str | None:
    for attachment in message.attachments:
        content_type = attachment.content_type or ""
        if content_type.startswith("image/"):
            return attachment.url
    return None


class ShowcaseCog(commands.Cog):
    """Reposts highly-reacted showcase messages to a hall-of-fame channel."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    def _settings(self) -> Settings:
        return self.bot.settings

    async def _fetch_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.exception("Failed to fetch showcase channel %s", channel_id)
            return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        settings = self._settings()
        source_channel_ids = settings.showcase_source_channel_ids
        target_channel_id = settings.showcase_target_channel_id
        if not source_channel_ids or target_channel_id is None:
            return
        if payload.channel_id not in source_channel_ids:
            return
        if str(payload.emoji) != settings.showcase_reaction_emoji:
            return

        source_channel = await self._fetch_channel(payload.channel_id)
        if source_channel is None:
            return

        try:
            message = await source_channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return

        # The author reacting to their own post shouldn't be what triggers a highlight.
        if payload.user_id == message.author.id:
            return

        reaction = next(
            (r for r in message.reactions if str(r.emoji) == settings.showcase_reaction_emoji),
            None,
        )
        if reaction is None:
            return

        # Only real votes count: not the author, not bots, not throwaway accounts made for the occasion.
        cutoff = discord.utils.utcnow() - MIN_VOTER_ACCOUNT_AGE
        votes = 0
        if reaction.count >= settings.showcase_threshold:
            async for user in reaction.users(limit=200):
                if user.id != message.author.id and not user.bot and user.created_at <= cutoff:
                    votes += 1

        if not showcase.should_highlight_message(
            reaction_count=votes,
            threshold=settings.showcase_threshold,
            channel_id=payload.channel_id,
            source_channel_ids=source_channel_ids,
            is_bot_author=message.author.bot,
        ):
            return

        if not await showcase.try_reserve_highlight(message.id):
            return  # already highlighted, or a concurrent reaction claimed it first

        target_channel = await self._fetch_channel(target_channel_id)
        if target_channel is None:
            return

        embed = build_showcase_highlight_embed(
            author_name=str(message.author),
            author_avatar_url=message.author.display_avatar.url,
            content=defang(message.content),
            image_url=_first_image_url(message),
            reaction_count=votes,
            reaction_emoji=settings.showcase_reaction_emoji,
            jump_url=message.jump_url,
        )

        posted = await target_channel.send(embed=embed)
        await showcase.set_highlight_message_id(message.id, posted.id)


def setup(bot: discord.Bot):
    bot.add_cog(ShowcaseCog(bot))
