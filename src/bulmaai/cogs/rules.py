import logging
import discord
from discord.ext import commands

from bulmaai.ui.rules_views import RulesLanguageView, build_rules_embeds
from bulmaai.utils.permissions import is_admin

log = logging.getLogger(__name__)


class RulesCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(RulesLanguageView())
        log.info("Persistent rules language view registered")

    rules = discord.SlashCommandGroup("rules", "Server rules management")

    @rules.command(name="setup", description="Post the server rules with language selection buttons")
    @discord.option("channel", description="Channel to post rules in (default: current)", required=False)
    async def setup_rules(
        self,
        ctx: discord.ApplicationContext,
        channel: discord.TextChannel = None,
    ):
        if not is_admin(ctx.author):
            return await ctx.respond("Only staff can set up rules.", ephemeral=True)

        target_channel = channel or ctx.channel

        await ctx.defer(ephemeral=True)

        embeds = build_rules_embeds("en")
        view = RulesLanguageView()

        await target_channel.send(embeds=embeds, view=view)

        await ctx.followup.send(
            f"✅ Rules posted in {target_channel.mention} with language selection buttons!",
            ephemeral=True,
        )

    @rules.command(name="preview", description="Preview rules in a specific language")
    @discord.option(
        "language",
        description="Language to preview",
        choices=["English", "Español", "Português"],
        required=True,
    )
    async def preview_rules(self, ctx: discord.ApplicationContext, language: str):
        lang_map = {"English": "en", "Español": "es", "Português": "pt"}
        lang_code = lang_map.get(language, "en")

        embeds = build_rules_embeds(lang_code)
        await ctx.respond(embeds=embeds, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(RulesCog(bot))

