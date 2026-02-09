import logging

import discord

from bulmaai.services.llm_client import llm_client


log = logging.getLogger(__name__)


class LLMMasterCog(discord.Cog):
    """Commands for interacting with the LLM."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # To be finished
        if message.author.bot:
            return

        if self.bot.user in message.mentions and message.channel.id == 1470178423862460510:
            log.info("Received message mentioning bot from %s: %s", message.author, message.content)

        pass

def setup(bot):
    bot.add_cog(LLMMasterCog(bot))