import logging
import discord
from discord.ext import commands

from bulmaai.ui.support_views import (
    SupportLanguageView,
    SupportLinkButtons,
    build_support_embeds,
)
from bulmaai.utils.permissions import is_admin

log = logging.getLogger(__name__)


class SupportUsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(SupportLanguageView())
        log.info("Persistent support-us language view registered")

    support = discord.SlashCommandGroup("supportus", "Support-us channel management")

    @support.command(name="setup", description="Post the support-us message with language selection buttons")
    @discord.option("channel", description="Channel to post in (default: current)", required=False)
    async def setup_support(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.TextChannel = None,
    ):
        if not is_admin(ctx.author):
            return await ctx.respond("Only staff can set up the support-us message.", ephemeral=True)

        target_channel = channel or ctx.channel

        await ctx.defer(ephemeral=True)

        embeds = build_support_embeds("en")
        link_view = SupportLinkButtons("en")

        # Send the English embed with Patreon/GitHub link buttons
        await target_channel.send(embeds=embeds, view=link_view)

        # Send the language selection buttons as a separate message
        lang_view = SupportLanguageView()
        await target_channel.send(view=lang_view)

        await ctx.followup.send(
            f"✅ Support-us message posted in {target_channel.mention} with language selection buttons!",
            ephemeral=True,
        )

    @support.command(name="preview", description="Preview the support-us message in a specific language")
    @discord.option(
        "language",
        description="Language to preview",
        choices=["English", "Español", "Português"],
        required=True,
    )
    async def preview_support(self, ctx: discord.ApplicationContext, language: str):
        lang_map = {"English": "en", "Español": "es", "Português": "pt"}
        lang_code = lang_map.get(language, "en")

        embeds = build_support_embeds(lang_code)
        link_view = SupportLinkButtons(lang_code)
        await ctx.respond(embeds=embeds, view=link_view, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(SupportUsCog(bot))

