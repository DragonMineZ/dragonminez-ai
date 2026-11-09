"""News intake: /news (admins or Bruno, English) manages the topics; a 5-minute loop claims the due ones and posts
their new items in news_channel_id (services/news_intake)."""

import asyncio
import logging

import discord
from discord.ext import tasks

from bulmaai.services import news_intake as news
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import is_admin, is_bruno

log = logging.getLogger(__name__)

DENIED_MSG = "Only admins can manage news tasks."
OFF_MSG = "The news intake is off, or its channel can't be found."
UNKNOWN_MSG = "No news task named `{name}`."
FAILED_MSG = "Couldn't reach the database right now."


class NewsIntakeCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._run_lock = asyncio.Lock()

    @property
    def settings(self):
        return self.bot.settings

    async def on_startup(self) -> None:
        if not self.poll_news.is_running():
            self.poll_news.start()

    async def on_shutdown(self) -> None:
        self.poll_news.cancel()

    def _allowed(self, user) -> bool:
        return is_bruno(user) or is_admin(user, settings=self.settings)

    async def _channel(self):
        settings = self.settings
        if not settings.news_enabled or not settings.news_channel_id:
            return None
        channel = self.bot.get_channel(settings.news_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(settings.news_channel_id)
            except discord.HTTPException:
                log.info("News channel %s can't be fetched", settings.news_channel_id)
                return None
        return channel if hasattr(channel, "send") else None

    # --- the loop ---------------------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def poll_news(self) -> None:
        try:
            channel = await self._channel()
            if channel is None:
                return
            async with self._run_lock:
                for task in await news.claim_due():
                    await news.run_task(channel, task, self.settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("News intake loop failed", exc_info=True)

    @poll_news.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # --- /news ------------------------------------------------------------------------------------

    news_group = discord.SlashCommandGroup("news", description="Admin: news topics the bot searches the web for")

    @news_group.command(name="add", description="Add a news topic to search for")
    @discord.option("name", str, description="Short id: a-z, 0-9 and -, up to 40 characters")
    @discord.option("prompt", str, description="What to look for, e.g. new Claude model releases from Anthropic")
    @discord.option("interval_hours", int, description="Hours between searches (default 6)", min_value=1, max_value=168, required=False)
    async def news_add(self, ctx: discord.ApplicationContext, name: str, prompt: str, interval_hours: int = 6):
        if not self._allowed(ctx.author):
            return await ctx.respond(DENIED_MSG, ephemeral=True)
        name, prompt = name.strip().lower(), prompt.strip()
        interval_hours = interval_hours or 6
        if error := news.validate_name(name) or news.validate_prompt(prompt):
            return await ctx.respond(error, ephemeral=True)
        if not news.INTERVAL_MIN <= interval_hours <= news.INTERVAL_MAX:
            return await ctx.respond("Interval must be 1-168 hours.", ephemeral=True)
        try:
            task = await news.add_task(name, prompt, interval_hours, ctx.author.id)
        except Exception:
            log.warning("/news add failed", exc_info=True)
            return await ctx.respond(FAILED_MSG, ephemeral=True)
        if task is None:
            return await ctx.respond(f"A news task named `{name}` already exists.", ephemeral=True)
        await ctx.respond(
            f"Added `{name}`: searched every {interval_hours}h, first run within 5 minutes.", ephemeral=True
        )

    @news_group.command(name="list", description="List the news topics")
    async def news_list(self, ctx: discord.ApplicationContext):
        if not self._allowed(ctx.author):
            return await ctx.respond(DENIED_MSG, ephemeral=True)
        try:
            items = await news.list_tasks()
        except Exception:
            log.warning("/news list failed", exc_info=True)
            return await ctx.respond(FAILED_MSG, ephemeral=True)
        await ctx.respond(view=news.tasks_card(items), ephemeral=True)

    async def _by_name(self, ctx, name: str, action) -> None:
        if not self._allowed(ctx.author):
            return await ctx.respond(DENIED_MSG, ephemeral=True)
        name = name.strip().lower()
        try:
            done = await action(name)
        except Exception:
            log.warning("/news %s failed", ctx.command.name, exc_info=True)
            return await ctx.respond(FAILED_MSG, ephemeral=True)
        if not done:
            return await ctx.respond(UNKNOWN_MSG.format(name=name[:40]), ephemeral=True)
        await ctx.respond(f"`{name}`: {ctx.command.name} done.", ephemeral=True)

    @news_group.command(name="remove", description="Delete a news topic and its history")
    @discord.option("name", str, description="The topic's name")
    async def news_remove(self, ctx: discord.ApplicationContext, name: str):
        await self._by_name(ctx, name, news.remove_task)

    @news_group.command(name="pause", description="Stop searching a news topic")
    @discord.option("name", str, description="The topic's name")
    async def news_pause(self, ctx: discord.ApplicationContext, name: str):
        await self._by_name(ctx, name, lambda value: news.set_enabled(value, False))

    @news_group.command(name="resume", description="Search a paused news topic again")
    @discord.option("name", str, description="The topic's name")
    async def news_resume(self, ctx: discord.ApplicationContext, name: str):
        await self._by_name(ctx, name, lambda value: news.set_enabled(value, True))

    @news_group.command(name="run", description="Search a news topic now")
    @discord.option("name", str, description="The topic's name")
    async def news_run(self, ctx: discord.ApplicationContext, name: str):
        if not self._allowed(ctx.author):
            return await ctx.respond(DENIED_MSG, ephemeral=True)
        await ctx.defer(ephemeral=True)
        name = name.strip().lower()
        channel = await self._channel()
        if channel is None:
            return await ctx.followup.send(OFF_MSG, ephemeral=True)
        try:
            async with self._run_lock:
                task = await news.claim_now(name)
                if task is None:
                    return await ctx.followup.send(UNKNOWN_MSG.format(name=name[:40]), ephemeral=True)
                posted, error = await news.run_task(channel, task, self.settings)
        except Exception:
            log.warning("/news run failed", exc_info=True)
            return await ctx.followup.send(FAILED_MSG, ephemeral=True)
        if error:
            return await ctx.followup.send(f"Failed: {error}", ephemeral=True)
        await ctx.followup.send(f"Posted {posted} new item(s).", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(NewsIntakeCog(bot))
