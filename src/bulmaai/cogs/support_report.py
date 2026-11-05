"""Weekly staff support report: posted on Mondays from 09:00 UTC to support_report_channel_id, and on demand with
/support-report. The schedule lives in the DB (support_report_posts.posted_at), so restarts neither skip nor repeat
a week; claim_post makes sure only one loop posts. Right after the weekly post (same claim, never on demand) come the
"Answers to fix" cards: the week's 👎 answers, handled in cogs/support_info.py."""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from bulmaai.services import ai_feedback as feedback_store
from bulmaai.services import support_report as report
from bulmaai.ui import feedback_fix
from bulmaai.ui.support_report_views import support_report_card
from bulmaai.utils.i18n import described, reader_language, t, table
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import in_main_guild, is_staff

log = logging.getLogger(__name__)

TEXT = table({
    "en": {"staff_only": "Only staff can use this.", "failed": "Couldn't build the report. Try again."},
    "es": {"staff_only": "Solo el staff puede usar esto.", "failed": "No pude armar el reporte. Inténtalo de nuevo."},
    "pt": {"staff_only": "Só a staff pode usar isso.", "failed": "Não consegui montar o relatório. Tente novamente."},
})
DESC = table({
    "en": {"support-report": "Staff: the support report for the last 7 days"},
    "es": {"support-report": "Staff: el reporte de soporte de los últimos 7 días"},
    "pt": {"support-report": "Staff: o relatório de suporte dos últimos 7 dias"},
})
MAX_ANSWERS_TO_FIX = 10


def _is_staff_member(user, settings) -> bool:
    return isinstance(user, discord.Member) and in_main_guild(user, settings) and is_staff(user, settings=settings)


async def build_card(settings, now: datetime, language: str = "en") -> discord.ui.DesignerView:
    """The report for the 7 days before now; the numbers always come, the AI sections only when the call works."""
    data = await report.collect(now - report.WINDOW, now)
    return support_report_card(data, await report.generate_insights(data, settings), language)


class SupportReportCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._warned_channel = False

    async def on_startup(self) -> None:
        if not self.weekly_post.is_running():
            self.weekly_post.start()

    async def on_shutdown(self) -> None:
        self.weekly_post.cancel()

    @tasks.loop(hours=1)
    async def weekly_post(self) -> None:
        try:
            await self._post_tick(datetime.now(timezone.utc))
        except Exception:
            log.exception("Weekly support report failed")

    @weekly_post.before_loop
    async def _before_weekly_post(self) -> None:
        await self.bot.wait_until_ready()

    def _warn_once(self, text: str) -> None:
        if not self._warned_channel:
            self._warned_channel = True
            log.info(text)

    async def _post_tick(self, now: datetime) -> None:
        channel_id = self.bot.settings.support_report_channel_id
        if not channel_id or not report.report_due(now, await report.last_posted()):
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return self._warn_once(f"Weekly support report: channel {channel_id} not found; skipping")
        claimed, previous = await report.claim_post()
        if not claimed:
            return
        try:
            view = await build_card(self.bot.settings, now)
            await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.NotFound):
            # Retrying every hour would only repeat the AI call; the week is skipped.
            return self._warn_once(f"Weekly support report: can't post in channel {channel_id}; skipping this week")
        except BaseException:
            await report.release_claim(previous)
            raise
        await self._post_answers_to_fix(channel, now)

    def _where(self, channel_id: int) -> str:
        channel = self.bot.get_channel(channel_id)
        return "DM" if isinstance(channel, discord.abc.PrivateChannel) else f"<#{channel_id}>"

    async def _post_answers_to_fix(self, channel, now: datetime) -> None:
        """The week's 👎 answers nobody reviewed (newest first, up to MAX_ANSWERS_TO_FIX): a header card, then one card
        per answer. Nothing when there are none. The report is out already, so a failure here is only logged: it
        must never release the claim and post the report twice."""
        try:
            items = await feedback_store.answers_to_fix(now - report.WINDOW, now, limit=MAX_ANSWERS_TO_FIX)
            if not items:
                return
            none = discord.AllowedMentions.none()
            await channel.send(view=feedback_fix.header_card(len(items)), allowed_mentions=none)
            for item in items:
                await channel.send(view=feedback_fix.item_card(item, self._where(item.channel_id)), allowed_mentions=none)
        except Exception:
            log.exception("Answers to fix failed")

    @discord.slash_command(name="support-report", **described(DESC, "support-report"))
    async def support_report(self, ctx: discord.ApplicationContext):
        language = reader_language(ctx, self.bot.settings)
        if not _is_staff_member(ctx.author, self.bot.settings):
            return await ctx.respond(t(TEXT, "staff_only", language), ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            view = await build_card(self.bot.settings, datetime.now(timezone.utc), language)
        except Exception:
            log.exception("/support-report failed")
            return await ctx.followup.send(t(TEXT, "failed", language), ephemeral=True)
        await ctx.followup.send(view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def setup(bot: discord.Bot) -> None:
    bot.add_cog(SupportReportCog(bot))
