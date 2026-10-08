import logging

import discord
from discord.ext import commands

from bulmaai.ui.log_help_views import build_log_help_card
from bulmaai.ui.v2 import card


log = logging.getLogger(__name__)


class MetaCog(commands.Cog):
    """Meta (Utility) commands for the bot."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="about", description="Get information about the bot.")
    async def about(self, ctx: discord.ApplicationContext):
        about = (
            "## About BulmaAI\n"
            "BulmaAI helps the DragonMineZ server with support, announcements, logs, and staff tooling.\n"
            "**Version** 1.8.4　**Author** DragonMineZ Team"
        )
        await ctx.respond(view=card(about, color=discord.Color.blue()))

    @discord.slash_command(
        name="loghelp",
        description="Post info on finding latest.log or crash-report.txt",
    )
    @discord.option(
        "language",
        description="Language to post",
        choices=["English", "Espanol", "Portugues"],
        required=False,
    )
    async def loghelp(self, ctx: discord.ApplicationContext, language: str = "English"):
        lang_map = {"English": "en", "Espanol": "es", "Portugues": "pt"}
        lang_code = lang_map.get(language, "en")
        await ctx.respond(view=build_log_help_card(lang_code))


def setup(bot: discord.Bot):
    bot.add_cog(MetaCog(bot))
