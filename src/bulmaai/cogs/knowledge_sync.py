"""Keeps the support AI's knowledge (knowledge_docs) in sync: the wiki every SYNC_HOURS, plus once per start the
curated data/knowledge/*.md files and the one-time backfill of tickets the old OpenAI store had learned from.
Every step is idempotent, so restarts and reloads just redo it."""

import logging
from pathlib import Path

import discord
from discord.ext import tasks

from bulmaai.services import knowledge
from bulmaai.services.ticket_transcripts import backfill_ticket_knowledge
from bulmaai.services.wiki_knowledge import sync_wiki_knowledge
from bulmaai.utils.lifecycle import ReloadableCog

log = logging.getLogger(__name__)

SYNC_HOURS = 6
LOCAL_KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "data" / "knowledge"


class KnowledgeSyncCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    async def on_startup(self) -> None:
        if not self.wiki_sync.is_running():
            self.wiki_sync.start()

    async def on_shutdown(self) -> None:
        self.wiki_sync.cancel()

    @tasks.loop(hours=SYNC_HOURS)
    async def wiki_sync(self) -> None:
        settings = self.bot.settings
        try:
            result = await sync_wiki_knowledge(settings.wiki_api_url, base_url=settings.wiki_base_url)
            log.info(
                "Wiki knowledge sync: uploaded %d, deleted %d, unchanged %d",
                result["uploaded"], result["deleted"], result["unchanged"],
            )
        except Exception:
            log.exception("Wiki knowledge sync failed")

    @wiki_sync.before_loop
    async def _before_wiki_sync(self) -> None:
        await self.bot.wait_until_ready()
        try:
            result = await knowledge.sync_local_docs(LOCAL_KNOWLEDGE_DIR)
            log.info(
                "Local knowledge sync: uploaded %d, deleted %d, unchanged %d",
                result["uploaded"], result["deleted"], result["unchanged"],
            )
        except Exception:
            log.exception("Local knowledge sync failed")
        try:
            moved = await backfill_ticket_knowledge()
            if moved:
                log.info("Backfilled %d ticket knowledge docs", moved)
        except Exception:
            log.exception("Ticket knowledge backfill failed")


def setup(bot: discord.Bot) -> None:
    bot.add_cog(KnowledgeSyncCog(bot))
