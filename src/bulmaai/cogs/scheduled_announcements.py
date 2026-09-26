"""Background sender for panel-scheduled announcements (see web/routes_announce.py).

Polls every POLL_SECONDS (<=30s granularity, per spec) for due rows and sends them through
services.panel_announcements.send_scheduled, which does the row-lock claim so a row is never
sent twice even if two workers raced. Runs on startup too, so anything due while the bot was
down goes out then, unless it's overdue by more than OVERDUE_AFTER (marked failed instead).
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from bulmaai.database.db import get_pool
from bulmaai.services.panel_announcements import OVERDUE_AFTER, mark_overdue, send_scheduled

log = logging.getLogger(__name__)

POLL_SECONDS = 20


class ScheduledAnnouncementsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.send_due.is_running():
            self.send_due.start()

    def cog_unload(self) -> None:
        self.send_due.cancel()

    @tasks.loop(seconds=POLL_SECONDS)
    async def send_due(self) -> None:
        try:
            await self._tick()
        except Exception:
            log.exception("Scheduled-announcements tick failed")

    @send_due.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick(self) -> None:
        pool = await get_pool()
        due = await pool.fetch(
            "SELECT id, send_at FROM panel_announcements WHERE status = 'scheduled' AND send_at <= now() "
            "ORDER BY send_at ASC"
        )
        now = datetime.now(timezone.utc)
        for row in due:
            if now - row["send_at"] > OVERDUE_AFTER:
                await mark_overdue(pool, row["id"], row["send_at"])
                log.warning("Scheduled announcement %s skipped: overdue by more than %s", row["id"], OVERDUE_AFTER)
                continue
            result = await send_scheduled(self.bot, row["id"])
            if result["status"] != "sent":
                log.warning("Scheduled announcement %s failed: %s", row["id"], result.get("error"))


def setup(bot: discord.Bot) -> None:
    bot.add_cog(ScheduledAnnouncementsCog(bot))
