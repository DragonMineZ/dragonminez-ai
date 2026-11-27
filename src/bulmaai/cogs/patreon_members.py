import logging

import discord
from discord.ext import tasks

from bulmaai.services import patreon_members
from bulmaai.utils.lifecycle import ReloadableCog

log = logging.getLogger(__name__)

SYNC_INTERVAL_MINUTES = 60


class PatreonMembersCog(ReloadableCog):
    """Keeps patreon_members (campaign members by their connected Discord account) in step with Patreon. Read-only:
    it changes no roles or whitelist entries."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    async def on_startup(self) -> None:
        settings = self.bot.settings
        if not settings.PATREON_CREATOR_TOKEN or not settings.PATREON_CAMPAIGN_ID:
            log.info("Patreon creator token or campaign id missing; member sync is off")
            return
        if not self.sync_members.is_running():
            self.sync_members.start()

    async def on_shutdown(self) -> None:
        self.sync_members.cancel()

    @tasks.loop(minutes=SYNC_INTERVAL_MINUTES)
    async def sync_members(self) -> None:
        await self.sync_once()

    async def sync_once(self) -> int | None:
        """Members stored, or None when the sync failed or was skipped (the stored list is then left alone)."""
        settings = self.bot.settings
        try:
            members = await patreon_members.fetch_all_members(
                creator_token=settings.PATREON_CREATOR_TOKEN, campaign_id=settings.PATREON_CAMPAIGN_ID
            )
        except Exception as exc:
            log.warning("Patreon member sync failed: %s", exc)
            return None
        if not members:
            # Never wipe the table on an empty answer (a token missing a scope reads as nobody connected).
            log.warning("Patreon member sync found no members with Discord connected; kept the stored list")
            return None
        await patreon_members.replace_all(members)
        log.info("Patreon member sync stored %d members", len(members))
        return len(members)

    @sync_members.before_loop
    async def _before_sync_members(self) -> None:
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot) -> None:
    bot.add_cog(PatreonMembersCog(bot))
