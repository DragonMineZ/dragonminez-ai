import io
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI

from bulmaai.config import load_settings

log = logging.getLogger(__name__)

load_dotenv()
settings = load_settings()

client = AsyncOpenAI(api_key=settings.openai_key)

ANNOUNCEMENT_SOURCE_CHANNEL_ID = 1260409720733175838  # English announcements channel ID
SPANISH_TARGET_CHANNEL_ID = 1280350384992288778       # Spanish announcements channel ID
PORTUGUESE_TARGET_CHANNEL_ID = 1472964446866636892    # Portuguese announcements channel ID

# Language role IDs for mention swapping in translations
ENGLISH_ROLE_ID = 1260413114898317387
SPANISH_ROLE_ID = 1260413006202802276
PORTUGUESE_ROLE_ID = 1469153940749680821

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


def swap_role_mentions(text: str, target_language: str) -> str:
    """Replace the English lang role mention with the target language's role mention."""
    if ENGLISH_ROLE_ID is None:
        return text
    english_mention = f"<@&{ENGLISH_ROLE_ID}>"
    if target_language == "es":
        return text.replace(english_mention, f"<@&{SPANISH_ROLE_ID}>")
    elif target_language == "pt":
        return text.replace(english_mention, f"<@&{PORTUGUESE_ROLE_ID}>")
    return text


async def translate_text(text: str, target_language: str) -> str:
    language_name = "Spanish" if target_language == "es" else "Brazilian Portuguese"

    response = await client.responses.create(
        model=settings.openai_translation_model,
        instructions=f"{TRANSLATION_INSTRUCTIONS}\n\nTranslate to {language_name}.",
        input=text,
        text={"verbosity": "medium"},
    )

    return response.output_text.strip()


class AiAnnTranslation(commands.Cog):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.author.guild_permissions.administrator:
            return

        if message.channel.id != ANNOUNCEMENT_SOURCE_CHANNEL_ID:
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
                spanish_text = await translate_text(message.content, "es")
                portuguese_text = await translate_text(message.content, "pt")

                spanish_text = swap_role_mentions(spanish_text, "es")
                portuguese_text = swap_role_mentions(portuguese_text, "pt")
            else:
                spanish_text = ""
                portuguese_text = ""

            spanish_channel = self.bot.get_channel(SPANISH_TARGET_CHANNEL_ID)
            portuguese_channel = self.bot.get_channel(PORTUGUESE_TARGET_CHANNEL_ID)

            role_mentions_only = discord.AllowedMentions(roles=True, users=False, everyone=False)

            if spanish_channel:
                await spanish_channel.send(
                    spanish_text or None, files=files_for_spanish, allowed_mentions=role_mentions_only
                )
                log.info("Spanish translation sent successfully")
            else:
                log.warning(f"Spanish channel {SPANISH_TARGET_CHANNEL_ID} not found")

            if portuguese_channel:
                await portuguese_channel.send(
                    portuguese_text or None, files=files_for_portuguese, allowed_mentions=role_mentions_only
                )
                log.info("Portuguese translation sent successfully")
            else:
                log.warning(f"Portuguese channel {PORTUGUESE_TARGET_CHANNEL_ID} not found")

        except Exception as e:
            log.error(f"Failed to translate announcement: {e}", exc_info=True)

    @commands.Cog.listener(name="on_message")
    async def on_message_publish(self, message: discord.Message):
        # Publish announcement messages automatically from the three announcement channels + releases + sneak peeks to the public
        RELEASES_CHANNEL_ID = 1260409841424535624
        SNEAK_PEEKS_CHANNEL_ID = 1280350775989637130

        if message.channel.id in {ANNOUNCEMENT_SOURCE_CHANNEL_ID, SPANISH_TARGET_CHANNEL_ID, PORTUGUESE_TARGET_CHANNEL_ID, RELEASES_CHANNEL_ID, SNEAK_PEEKS_CHANNEL_ID}:
            try:
                # Note: Forcefully publish bot-made messages because the translation messages are sent by the bot (hehe)
                await message.publish()
                log.info(f"Published announcement message from {message.author}")
            except Exception as e:
                log.error(f"Failed to publish announcement message: {e}", exc_info=True)


def setup(bot: discord.Bot):
    bot.add_cog(AiAnnTranslation(bot))

