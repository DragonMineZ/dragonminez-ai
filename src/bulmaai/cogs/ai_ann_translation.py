import io
import logging

import discord
from discord.ext import commands
from openai import AsyncOpenAI

log = logging.getLogger(__name__)

TRANSLATION_INSTRUCTIONS = """
You are a professional translator for a Minecraft Dragon Ball Z mod called DragonMineZ.
Translate the announcement naturally and engagingly while preserving:
- Gaming terminology and mod-specific terms (keep technical names in English if commonly used) (Roadmap will be roadmap too)
- Emojis and formatting (Discord markdown)
- The tone and excitement of the original message
- Any links, mentions, or Discord formatting exactly as they appear.

Do NOT add any extra commentary, just provide the translation.
Be AWARE of the 2000 character limit for Discord messages and truncate if necessary, but try to keep the full content if possible.
"""


def swap_role_mentions(text: str, target_language: str, cog: "AiAnnTranslation") -> str:
    """Replace the English lang role mention with the target language's role mention."""
    if cog.settings.announcement_role_en_id is None:
        return text
    english_mention = f"<@&{cog.settings.announcement_role_en_id}>"
    if target_language == "es":
        if cog.settings.announcement_role_es_id is None:
            return text
        return text.replace(english_mention, f"<@&{cog.settings.announcement_role_es_id}>")
    elif target_language == "pt":
        if cog.settings.announcement_role_pt_id is None:
            return text
        return text.replace(english_mention, f"<@&{cog.settings.announcement_role_pt_id}>")
    return text


async def translate_text(cog: "AiAnnTranslation", text: str, target_language: str) -> str:
    language_name = "Spanish" if target_language == "es" else "Brazilian Portuguese"

    response = await cog.client.responses.create(
        model=cog.settings.openai_translation_model,
        instructions=f"{TRANSLATION_INSTRUCTIONS}\n\nTranslate to {language_name}.",
        input=text,
        text={"verbosity": "medium"},
    )

    return response.output_text.strip()


class AiAnnTranslation(commands.Cog):

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.client = AsyncOpenAI(api_key=self.settings.openai_key)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.author.guild_permissions.administrator:
            return

        if message.channel.id != self.settings.announcement_source_channel_id:
            return

        if not message.content.strip() and not message.attachments:
            return

        log.info(f"Translating announcement from {message.author}: {message.content[:50]}...")

        try:
            files_for_spanish = []
            files_for_portuguese = []
            for attachment in message.attachments:
                try:
                    file_bytes = await attachment.read()
                    files_for_spanish.append(
                        discord.File(io.BytesIO(file_bytes), filename=attachment.filename, spoiler=attachment.is_spoiler())
                    )
                    files_for_portuguese.append(
                        discord.File(io.BytesIO(file_bytes), filename=attachment.filename, spoiler=attachment.is_spoiler())
                    )
                    log.info(f"Downloaded attachment: {attachment.filename}")
                except Exception as e:
                    log.warning(f"Failed to download attachment {attachment.filename}: {e}")

            if message.content.strip():
                spanish_text = await translate_text(self, message.content, "es")
                portuguese_text = await translate_text(self, message.content, "pt")

                spanish_text = swap_role_mentions(spanish_text, "es", self)
                portuguese_text = swap_role_mentions(portuguese_text, "pt", self)
            else:
                spanish_text = ""
                portuguese_text = ""

            spanish_channel = self.bot.get_channel(self.settings.announcement_spanish_channel_id)
            portuguese_channel = self.bot.get_channel(self.settings.announcement_portuguese_channel_id)

            role_mentions_only = discord.AllowedMentions(roles=True, users=False, everyone=False)

            if spanish_channel:
                await spanish_channel.send(
                    spanish_text or None, files=files_for_spanish, allowed_mentions=role_mentions_only
                )
                log.info("Spanish translation sent successfully")
            else:
                log.warning(
                    "Spanish channel %s not found",
                    self.settings.announcement_spanish_channel_id,
                )

            if portuguese_channel:
                await portuguese_channel.send(
                    portuguese_text or None, files=files_for_portuguese, allowed_mentions=role_mentions_only
                )
                log.info("Portuguese translation sent successfully")
            else:
                log.warning(
                    "Portuguese channel %s not found",
                    self.settings.announcement_portuguese_channel_id,
                )

        except Exception as e:
            log.error(f"Failed to translate announcement: {e}", exc_info=True)

    @commands.Cog.listener(name="on_message")
    async def on_message_publish(self, message: discord.Message):
        publishable_channels = {
            self.settings.announcement_source_channel_id,
            self.settings.announcement_spanish_channel_id,
            self.settings.announcement_portuguese_channel_id,
            self.settings.releases_channel_id,
            self.settings.sneak_peeks_channel_id,
            self.settings.patreon_announcement_channel_id,
        }
        publishable_channels.discard(None)

        if message.channel.id in publishable_channels:
            try:
                await message.publish()
                log.info(f"Published announcement message from {message.author}")
            except Exception as e:
                log.error(f"Failed to publish announcement message: {e}", exc_info=True)


def setup(bot: discord.Bot):
    bot.add_cog(AiAnnTranslation(bot))

