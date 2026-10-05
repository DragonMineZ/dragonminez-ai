import logging

import discord
from discord.ext import commands

from bulmaai.ui.rules_views import RulesLanguageView
from bulmaai.utils.lifecycle import ReloadableCog

log = logging.getLogger(__name__)


class RulesCog(ReloadableCog):
    """Keeps the posted rules message's language buttons working; the text and posting live in the web panel."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    async def on_startup(self) -> None:
        self.bot.add_view(RulesLanguageView())
        log.info("Persistent rules language view registered")


def setup(bot: discord.Bot):
    bot.add_cog(RulesCog(bot))
