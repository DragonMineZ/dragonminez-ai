from typing import TypedDict

import discord

from bulmaai.services.message_presets import get_rules_content


class RuleSection(TypedDict):
    title: str | None
    content: str


class RulesLanguageData(TypedDict):
    title: str
    sections: list[RuleSection]


def build_rules_embeds(language: str = "en") -> list[discord.Embed]:
    content = get_rules_content()
    data: RulesLanguageData = content.get(language, content["en"])
    embeds: list[discord.Embed] = []

    colors = [
        discord.Color.from_rgb(88, 101, 242),
        discord.Color.from_rgb(237, 66, 69),
        discord.Color.from_rgb(237, 66, 69),
        discord.Color.from_rgb(237, 66, 69),
        discord.Color.from_rgb(237, 66, 69),
    ]

    for index, section in enumerate(data["sections"]):
        embed = discord.Embed(
            color=colors[index] if index < len(colors) else discord.Color.blurple()
        )
        if index == 0:
            embed.title = data["title"]
        if section["title"]:
            embed.add_field(name=section["title"], value=section["content"], inline=False)
        else:
            embed.description = section["content"]
        embeds.append(embed)

    return embeds


class RulesLanguageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _swap_language(self, interaction: discord.Interaction, language: str) -> None:
        await interaction.response.edit_message(
            embeds=build_rules_embeds(language),
            view=self,
        )

    @discord.ui.button(
        label="English",
        style=discord.ButtonStyle.primary,
        custom_id="rules_lang:en",
        emoji="🇺🇸",
    )
    async def english_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._swap_language(interaction, "en")

    @discord.ui.button(
        label="Español",
        style=discord.ButtonStyle.secondary,
        custom_id="rules_lang:es",
        emoji="🇪🇸",
    )
    async def spanish_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._swap_language(interaction, "es")

    @discord.ui.button(
        label="Português",
        style=discord.ButtonStyle.secondary,
        custom_id="rules_lang:pt",
        emoji="🇧🇷",
    )
    async def portuguese_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._swap_language(interaction, "pt")
