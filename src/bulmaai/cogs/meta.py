import logging

import discord
from discord.ext import commands

from bulmaai.ui.log_help_views import build_log_help_embeds


log = logging.getLogger(__name__)


class MetaCog(commands.Cog):
    """Meta (Utility) commands for the bot."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="about", description="Get information about the bot.")
    async def about(self, ctx: discord.ApplicationContext):
        embed = discord.Embed(
            title="About BulmaAI",
            description="BulmaAI helps the DragonMineZ server with support, announcements, logs, and staff tooling.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Version", value="1.8.4", inline=False)
        embed.add_field(name="Author", value="DragonMineZ Team", inline=False)
        await ctx.respond(embed=embed)

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
        embeds = build_log_help_embeds(lang_code)
        await ctx.respond(embeds=embeds)


def setup(bot: discord.Bot):
    bot.add_cog(MetaCog(bot))
