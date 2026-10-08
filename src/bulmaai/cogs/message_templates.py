import logging

import discord
from discord.ext import commands

from bulmaai.services.cards import LANGUAGE_PREFIX, build_card_view, language_row
from bulmaai.services.message_templates import LANGUAGES, get_template

log = logging.getLogger(__name__)


class MessageTemplatesCog(commands.Cog):
    """Language buttons on posted templates: each click shows the clicker a private copy in that language.
    The custom id carries the template, so posts keep working after restarts. Posting lives in the panel."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        parts = str((interaction.data or {}).get("custom_id") or "").split(":")
        if len(parts) != 3 or parts[0] != LANGUAGE_PREFIX or parts[2] not in LANGUAGES:
            return
        _, template_id, lang = parts
        try:
            await self._show(interaction, template_id, lang)
        except Exception:
            log.exception("Language button %s failed", interaction.data)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong.", ephemeral=True)

    async def _show(self, interaction: discord.Interaction, template_id: str, lang: str) -> None:
        template = get_template(template_id)
        if template is None:
            await interaction.response.send_message("This message is outdated.", ephemeral=True)
            return
        card = template["languages"].get(lang) or template["languages"]["en"]
        view = build_card_view(card, extra_rows=[language_row(template_id)])
        if interaction.message and getattr(interaction.message.flags, "ephemeral", False):
            await interaction.response.edit_message(view=view, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(
                view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )


def setup(bot: discord.Bot):
    bot.add_cog(MessageTemplatesCog(bot))
