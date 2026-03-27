import logging

import discord
from discord.ext import commands

from bulmaai.services.message_presets import get_rules_content, update_rules_section
from bulmaai.ui.rules_views import RulesLanguageView, build_rules_embeds
from bulmaai.utils.permissions import is_admin

log = logging.getLogger(__name__)

LANGUAGE_MAP = {"English": "en", "Español": "es", "Português": "pt"}


class RulesEditModal(discord.ui.Modal):
    def __init__(self, *, language: str, section_index: int):
        super().__init__(title=f"Edit Rules {language.upper()} Section {section_index + 1}")
        rules = get_rules_content().get(language, get_rules_content()["en"])
        section = rules["sections"][section_index]
        self.language = language
        self.section_index = section_index

        self.title_input = discord.ui.InputText(
            label="Section Title",
            required=False,
            max_length=256,
            value=section.get("title") or "",
        )
        self.content_input = discord.ui.InputText(
            label="Section Content",
            style=discord.InputTextStyle.long,
            required=True,
            max_length=4000,
            value=section["content"],
        )
        self.add_item(self.title_input)
        self.add_item(self.content_input)

    async def callback(self, interaction: discord.Interaction):
        update_rules_section(
            self.language,
            self.section_index,
            title=self.title_input.value.strip() or None,
            content=self.content_input.value.strip(),
        )
        await interaction.response.send_message(
            f"Updated rules preset for `{self.language}` section {self.section_index + 1}.",
            embeds=build_rules_embeds(self.language),
            ephemeral=True,
        )


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
        await target_channel.send(embeds=build_rules_embeds("en"), view=RulesLanguageView())
        await ctx.followup.send(
            f"Rules posted in {target_channel.mention}.",
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
        lang_code = LANGUAGE_MAP.get(language, "en")
        await ctx.respond(embeds=build_rules_embeds(lang_code), ephemeral=True)

    @rules.command(name="edit", description="Edit a rules section and preview the result")
    @discord.option(
        "language",
        description="Language to edit",
        choices=["English", "Español", "Português"],
        required=True,
    )
    @discord.option(
        "section",
        description="Section number to edit",
        min_value=1,
        max_value=5,
        required=True,
    )
    async def edit_rules(self, ctx: discord.ApplicationContext, language: str, section: int):
        if not is_admin(ctx.author):
            return await ctx.respond("Only staff can edit rules presets.", ephemeral=True)
        lang_code = LANGUAGE_MAP.get(language, "en")
        await ctx.send_modal(RulesEditModal(language=lang_code, section_index=section - 1))


def setup(bot: discord.Bot):
    bot.add_cog(RulesCog(bot))
