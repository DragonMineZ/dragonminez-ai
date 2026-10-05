import logging

import discord
from discord.ext import commands

from bulmaai.ui.support_views import SupportPresetView

log = logging.getLogger(__name__)


class SupportUsCog(commands.Cog):
    """Keeps the posted support-us message's buttons working; the text and posting live in the web panel."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(SupportPresetView())
        log.info("Persistent support-us preset view registered")


def setup(bot: discord.Bot):
    bot.add_cog(SupportUsCog(bot))
