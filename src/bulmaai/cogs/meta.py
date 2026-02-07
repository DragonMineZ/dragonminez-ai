import logging

import discord
import time

from discord import slash_command

from bulmaai.services.llm_client import llm_client


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
            description="BulmaAI is a Discord bot, yay.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Version", value="1.0.0", inline=False)
        embed.add_field(name="Author", value="DragonMineZ Team", inline=False)
        await ctx.respond(embed=embed)

    @slash_command(
        name="ai_test",
        description="Test the LLM integration.",
        hidden=True,
        default_member_permissions=discord.Permissions(administrator=True),
    )
    async def ai_test(
            self,
            ctx: discord.ApplicationContext,
            question: str,
    ):
        await ctx.defer()

        messages = [
            {"role": "system", "content": "You are a helpful assistant. Try to always answer questions in 1900 characters or less."}, # TODO: Adjust system prompt as needed.
            {"role": "user", "content": question},
        ]

        try:
            response_text = await llm_client.chat(messages)
            prefix = "LLM response: "
            max_message_length = 2000
            available_length = max_message_length - len(prefix)
            if len(response_text) > available_length:
                response_text = response_text[:available_length]
            await ctx.followup.send(f"{prefix}{response_text}")
        except Exception as exc:
            log.exception("LLM test command failed: %s", exc)
            await ctx.followup.send("LLM test failed.")


def setup(bot):
    bot.add_cog(MetaCog(bot))
