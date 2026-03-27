import logging

import discord
from discord.ext import commands

from bulmaai.services.message_presets import get_support_content, update_support_field
from bulmaai.ui.support_views import SupportPresetView, build_support_embeds
from bulmaai.utils.permissions import is_admin

log = logging.getLogger(__name__)

LANGUAGE_MAP = {"English": "en", "Español": "es", "Português": "pt"}
SUPPORT_FIELDS = [
    "description",
    "perks_title",
    "perks_value",
    "development_title",
    "development_value",
    "credits_title",
    "credits_value",
    "community_title",
    "community_value",
    "boosting_title",
    "boosting_description",
    "boost_tier1_title",
    "boost_tier1_value",
    "boost_tier2_title",
    "boost_tier2_value",
    "boost_tier3_title",
    "boost_tier3_value",
    "boosting_footer",
    "patreon_label",
    "github_label",
]


class SupportPresetEditModal(discord.ui.Modal):
    def __init__(self, *, language: str, field: str):
        super().__init__(title=f"Edit Support {language.upper()} {field}")
        support = get_support_content().get(language, get_support_content()["en"])
        self.language = language
        self.field = field
        self.value_input = discord.ui.InputText(
            label=field,
            style=discord.InputTextStyle.long,
            required=True,
            max_length=4000,
            value=support[field],
        )
        self.add_item(self.value_input)

    async def callback(self, interaction: discord.Interaction):
        update_support_field(self.language, self.field, self.value_input.value.strip())
        await interaction.response.send_message(
            f"Updated support preset field `{self.field}` for `{self.language}`.",
            embeds=build_support_embeds(self.language),
            view=SupportPresetView(self.language),
            ephemeral=True,
        )


class SupportUsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(SupportPresetView())
        log.info("Persistent support-us preset view registered")

    support = discord.SlashCommandGroup("supportus", "Support-us channel management")

    @support.command(name="setup", description="Post the support-us message with language buttons")
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
        await target_channel.send(embeds=build_support_embeds("en"), view=SupportPresetView("en"))
        await ctx.followup.send(
            f"Support-us message posted in {target_channel.mention}.",
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
        lang_code = LANGUAGE_MAP.get(language, "en")
        await ctx.respond(
            embeds=build_support_embeds(lang_code),
            view=SupportPresetView(lang_code),
            ephemeral=True,
        )

    @support.command(name="edit", description="Edit a support preset field and preview the result")
    @discord.option(
        "language",
        description="Language to edit",
        choices=["English", "Español", "Português"],
        required=True,
    )
    @discord.option(
        "field",
        description="Preset field to edit",
        choices=SUPPORT_FIELDS,
        required=True,
    )
    async def edit_support(self, ctx: discord.ApplicationContext, language: str, field: str):
        if not is_admin(ctx.author):
            return await ctx.respond("Only staff can edit support presets.", ephemeral=True)
        lang_code = LANGUAGE_MAP.get(language, "en")
        await ctx.send_modal(SupportPresetEditModal(language=lang_code, field=field))


def setup(bot: discord.Bot):
    bot.add_cog(SupportUsCog(bot))
