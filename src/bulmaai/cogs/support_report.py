"""Weekly staff support report: saved on Mondays from 09:00 UTC to support_reports (the panel's AI page shows it), and
built on demand with /support-report (ephemeral). The schedule lives in the DB (support_report_posts.posted_at), so
restarts neither skip nor repeat a week; claim_post makes sure only one loop saves it."""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from bulmaai.services import support_report as report
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


def _is_staff_member(user, settings) -> bool:
    return isinstance(user, discord.Member) and in_main_guild(user, settings) and is_staff(user, settings=settings)


async def build_card(settings, now: datetime, language: str = "en") -> discord.ui.DesignerView:
    """The report for the 7 days before now; the numbers always come, the AI sections only when the call works."""
    data = await report.collect(now - report.WINDOW, now)
    return support_report_card(data, await report.generate_insights(data, settings), language)


class SupportReportCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

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

    async def _post_tick(self, now: datetime) -> None:
        if not report.report_due(now, await report.last_posted()):
            return
        claimed, previous = await report.claim_post()
        if not claimed:
            return
        try:
            data = await report.collect(now - report.WINDOW, now)
            insights = await report.generate_insights(data, self.bot.settings)
            await report.save_report(data, insights)
        except BaseException:
            await report.release_claim(previous)
            raise

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
