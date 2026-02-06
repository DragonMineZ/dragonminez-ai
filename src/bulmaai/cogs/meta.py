import logging

import discord
import time

from discord import slash_command


log = logging.getLogger(__name__)

class MetaCog(discord.Cog):
    """Meta (Utility) commands for the bot."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @slash_command(name="ping", description="Check the bot's latency.")
    async def ping(self, ctx: discord.ApplicationContext):
        start_time = time.perf_counter()
        await ctx.respond("Pong!")
        end_time = time.perf_counter()
        latency = (end_time - start_time) * 1000  # Convert to milliseconds
        await ctx.edit(content=f"Pong! Latency: {latency:.2f} ms")

    @slash_command(name="about", description="Get information about the bot.")
    async def about(self, ctx: discord.ApplicationContext):
        embed = discord.Embed(
            title="About BulmaAI",
            description="BulmaAI is a Discord bot that helps manage and moderate your server.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Version", value="1.0.0", inline=False)
        embed.add_field(name="Author", value="BulmaAI Team", inline=False)
        await ctx.respond(embed=embed)


def setup(bot):
    bot.add_cog(MetaCog(bot))