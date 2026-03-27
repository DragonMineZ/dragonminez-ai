from typing import TypedDict

import discord

from bulmaai.services.message_presets import get_support_content

PATREON_URL = "https://www.patreon.com/DragonMineZ"
GITHUB_URL = "https://github.com/DragonMineZ"


class SupportLanguageData(TypedDict):
    flag: str
    title: str
    description: str
    perks_title: str
    perks_value: str
    development_title: str
    development_value: str
    credits_title: str
    credits_value: str
    community_title: str
    community_value: str
    boosting_title: str
    boosting_description: str
    boost_tier1_title: str
    boost_tier1_value: str
    boost_tier2_title: str
    boost_tier2_value: str
    boost_tier3_title: str
    boost_tier3_value: str
    boosting_footer: str
    patreon_label: str
    github_label: str


def build_support_embeds(language: str = "en") -> list[discord.Embed]:
    content = get_support_content()
    data: SupportLanguageData = content.get(language, content["en"])

    embed = discord.Embed(color=discord.Color.from_rgb(88, 101, 242))
    embed.title = f"{data['flag']} {data['title']}"
    embed.description = data["description"]
    embed.add_field(name=data["perks_title"], value=data["perks_value"], inline=True)
    embed.add_field(name=data["development_title"], value=data["development_value"], inline=True)
    embed.add_field(name=data["credits_title"], value=data["credits_value"], inline=True)
    embed.add_field(name=data["community_title"], value=data["community_value"], inline=False)

    boost_embed = discord.Embed(color=discord.Color.from_rgb(244, 127, 255))
    boost_embed.title = data["boosting_title"]
    boost_embed.description = data["boosting_description"]
    boost_embed.add_field(name=data["boost_tier1_title"], value=data["boost_tier1_value"], inline=False)
    boost_embed.add_field(name=data["boost_tier2_title"], value=data["boost_tier2_value"], inline=False)
    boost_embed.add_field(name=data["boost_tier3_title"], value=data["boost_tier3_value"], inline=False)
    boost_embed.set_footer(text=data["boosting_footer"])

    return [embed, boost_embed]


class SupportPresetView(discord.ui.View):
    def __init__(self, language: str = "en"):
        super().__init__(timeout=None)
        content = get_support_content()
        data: SupportLanguageData = content.get(language, content["en"])

        self.add_item(
            discord.ui.Button(
                label=data["patreon_label"],
                style=discord.ButtonStyle.link,
                url=PATREON_URL,
                emoji="🧡",
            )
        )
        self.add_item(
            discord.ui.Button(
                label=data["github_label"],
                style=discord.ButtonStyle.link,
                url=GITHUB_URL,
                emoji="🔗",
            )
        )

    @discord.ui.button(
        label="English",
        style=discord.ButtonStyle.primary,
        custom_id="support_lang:en",
        emoji="🇺🇸",
        row=1,
    )
    async def english_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embeds=build_support_embeds("en"),
            view=SupportPresetView("en"),
        )

    @discord.ui.button(
        label="Español",
        style=discord.ButtonStyle.secondary,
        custom_id="support_lang:es",
        emoji="🇪🇸",
        row=1,
    )
    async def spanish_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embeds=build_support_embeds("es"),
            view=SupportPresetView("es"),
        )

    @discord.ui.button(
        label="Português",
        style=discord.ButtonStyle.secondary,
        custom_id="support_lang:pt",
        emoji="🇧🇷",
        row=1,
    )
    async def portuguese_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embeds=build_support_embeds("pt"),
            view=SupportPresetView("pt"),
        )
