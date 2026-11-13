"""Daily AI metrics post: 00:10 UTC for the UTC day that ended, to ai_reports_channel_id, and on demand with
/ai-daily (today so far). One ai_daily_posts row per day is claimed before any work, so a second loop or a restart
can't post it twice; a day without activity is claimed and skipped."""

import asyncio
import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import tasks

from bulmaai.services import ai_daily, evals
from bulmaai.ui.ai_daily_chart import render_chart
from bulmaai.ui.ai_daily_views import daily_card, eval_text
from bulmaai.utils.i18n import described, reader_language, t, table
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import in_main_guild, is_staff

log = logging.getLogger(__name__)

# The bot runs from src/, evals.RUNS_DIR is relative to the repo root.
RUNS_DIR = Path(__file__).resolve().parents[3] / evals.RUNS_DIR
IMAGE = "ai-daily.png"

TEXT = table({
    "en": {"staff_only": "Only staff can use this.", "failed": "Couldn't build the AI daily. Try again."},
    "es": {"staff_only": "Solo el staff puede usar esto.", "failed": "No pude armar el resumen diario de IA. Inténtalo de nuevo."},
    "pt": {"staff_only": "Só a staff pode usar isso.", "failed": "Não consegui montar o resumo diário de IA. Tente novamente."},
})
DESC = table({
    "en": {"ai-daily": "Staff: today's AI metrics so far"},
    "es": {"ai-daily": "Staff: las métricas de IA de hoy hasta ahora"},
    "pt": {"ai-daily": "Staff: as métricas de IA de hoje até agora"},
})


def _is_staff_member(user, settings) -> bool:
    return isinstance(user, discord.Member) and in_main_guild(user, settings) and is_staff(user, settings=settings)


def _last_eval(language: str = "en") -> str | None:
    """The newest answers eval run as 'Last eval: 39% pass (13 Nov)'; None without a run or on any problem."""
    try:
        files = evals.run_files("answers", RUNS_DIR)
        if not files:
            return None
        run = evals.load_run(files[-1])
        percent = round(run["metrics"]["overall"]["pass"] * 100)
        return eval_text(percent, datetime.fromisoformat(run["created_at"]).date(), language)
    except Exception:
        return None


class AIDailyCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._warned = False

    async def on_startup(self) -> None:
        if not self.daily_post.is_running():
            self.daily_post.start()

    async def on_shutdown(self) -> None:
        self.daily_post.cancel()

    @tasks.loop(minutes=10)
    async def daily_post(self) -> None:
        try:
            await self._post_tick(datetime.now(timezone.utc))
        except Exception:
            log.exception("AI daily post failed")

    @daily_post.before_loop
    async def _before_daily_post(self) -> None:
        await self.bot.wait_until_ready()

    def _warn_once(self, text: str) -> None:
        if not self._warned:
            self._warned = True
            log.info(text)

    async def _card_and_file(self, data, language: str):
        png = await asyncio.to_thread(render_chart, data.points)
        eval_line = await asyncio.to_thread(_last_eval, language)
        view = daily_card(data, eval_line=eval_line, language=language, image=IMAGE)
        return view, discord.File(io.BytesIO(png), filename=IMAGE)

    async def _post_tick(self, now: datetime) -> None:
        channel_id = self.bot.settings.ai_reports_channel_id
        if not channel_id:
            return
        day = ai_daily.post_due(now)
        if day is None:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return self._warn_once(f"AI daily: channel {channel_id} not found; skipping")
        if not await ai_daily.claim(day):
            return
        try:
            data = await ai_daily.collect(day, self.bot.settings, now=now)
            if not data.has_activity:
                return
            view, file = await self._card_and_file(data, "en")
            await channel.send(view=view, file=file, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.NotFound):
            # Retrying every 10 minutes would never succeed; the day is skipped.
            return self._warn_once(f"AI daily: can't post in channel {channel_id}; skipping {day}")
        except BaseException:
            await ai_daily.release(day)
            raise

    @discord.slash_command(name="ai-daily", **described(DESC, "ai-daily"))
    async def ai_daily_command(self, ctx: discord.ApplicationContext):
        language = reader_language(ctx, self.bot.settings)
        if not _is_staff_member(ctx.author, self.bot.settings):
            return await ctx.respond(t(TEXT, "staff_only", language), ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            now = datetime.now(timezone.utc)
            data = await ai_daily.collect(now.date(), self.bot.settings, now=now)
            view, file = await self._card_and_file(data, language)
        except Exception:
            log.exception("/ai-daily failed")
            return await ctx.followup.send(t(TEXT, "failed", language), ephemeral=True)
        await ctx.followup.send(view=view, file=file, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def setup(bot: discord.Bot) -> None:
    bot.add_cog(AIDailyCog(bot))
