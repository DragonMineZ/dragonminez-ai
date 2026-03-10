import logging
from typing import Any

import discord
from discord.ext import commands
from openai import AsyncOpenAI

from bulmaai.config import load_settings
from bulmaai.services.openai_client import run_support_agent
from bulmaai.utils.permissions import is_staff, is_admin

log = logging.getLogger(__name__)
settings = load_settings()
vision_client = AsyncOpenAI(api_key=settings.openai_key)

TICKETS_CATEGORY_ID = 1262517992982315110

PATREON_ROLE_IDS = {
    1287877272224665640,
    1287877305259130900,
}

GENERAL_AI_CHANNEL_IDS: set[int] = set()
GENERAL_MIN_SIMILARITY = 0.70
TICKET_MIN_SIMILARITY = 0.45
LOG_ATTACHMENT_EXTENSIONS = (".log", ".txt")
IMAGE_ATTACHMENT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def _user_has_patreon_role(member: discord.Member) -> bool:
    return any(role.id in PATREON_ROLE_IDS for role in member.roles)


def _is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    return (
        isinstance(channel, discord.TextChannel)
        and channel.category
        and channel.category.id == TICKETS_CATEGORY_ID
    )


def _is_general_ai_channel(channel: discord.TextChannel) -> bool:
    return channel.id in GENERAL_AI_CHANNEL_IDS


def _wants_whitelist_flow(text: str) -> bool:
    lowered = text.lower()
    triggers = (
        "whitelist",
        "beta access",
        "patreon access",
        "patreon role",
        "acesso patreon",
        "acceso patreon",
    )
    return any(t in lowered for t in triggers)


def _contains_log_attachment(message: discord.Message) -> bool:
    for attachment in message.attachments:
        filename = attachment.filename.lower()
        if filename.endswith(LOG_ATTACHMENT_EXTENSIONS):
            return True
    return False


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_ATTACHMENT_EXTENSIONS)


def _extract_docs_similarity(tool_results: list[Any]) -> float | None:
    for entry in tool_results:
        if entry.get("name") != "docs_search":
            continue
        output = entry.get("output") or {}
        best = output.get("best_similarity")
        if isinstance(best, (int, float)):
            return float(best)
    return None


class AITicketsCog(commands.Cog):
    """AI triage / support for ticket and configured AI channels."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    async def _build_history(self, channel: discord.TextChannel, limit: int = 10) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        async for msg in channel.history(limit=limit, oldest_first=True):
            role = "assistant" if msg.author == self.bot.user else "user"
            history.append({"role": role, "content": msg.content})
        return history

    async def _extract_ticket_image_context(self, message: discord.Message) -> str:
        image_urls = [a.url for a in message.attachments if _is_image_attachment(a)]
        if not image_urls:
            return ""

        snippets: list[str] = []
        for url in image_urls[:2]:
            try:
                input_payload: Any = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Read this support screenshot and extract only actionable "
                                    "details (errors, warnings, version hints, symptoms)."
                                ),
                            },
                            {"type": "input_image", "image_url": url},
                        ],
                    }
                ]
                resp = await vision_client.responses.create(
                    model=settings.openai_model,
                    input=input_payload,
                )
                snippets.append((resp.output_text or "").strip())
            except Exception:
                log.exception("Failed to extract image context from %s", url)

        merged = "\n".join(s for s in snippets if s)
        return merged[:2000]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots, DMs, and staff
        if message.author.bot or not message.guild:
            return
        if is_staff(message.author):
            if is_admin(message.author):
                pass
            return

        if not isinstance(message.author, discord.Member):
            return

        # Patreon-only while this feature is in beta.
        # TODO: remove this role gate when AI support is ready for all users.
        if not _user_has_patreon_role(message.author):
            return

        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return

        in_ticket = _is_ticket_channel(channel)
        in_general_ai = _is_general_ai_channel(channel)
        if not in_ticket and not in_general_ai:
            return

        if message.content.startswith(("!", "/", ".")):
            return

        # Ignore AI processing for log uploads; LogParserCog handles these already.
        if _contains_log_attachment(message):
            return

        try:
            history = await self._build_history(channel, limit=10)
            if in_ticket:
                image_context = await self._extract_ticket_image_context(message)
                if image_context:
                    history.append(
                        {
                            "role": "user",
                            "content": f"[Image context extracted from attachment]\n{image_context}",
                        }
                    )
        except Exception as e:
            log.exception("Error preparing support context: %s", e)
            return

        enabled_tools = [
            "start_patreon_whitelist_flow" if _wants_whitelist_flow(message.content) else "docs_search"
        ]

        try:
            result = await run_support_agent(
                messages=history,
                enabled_tools=enabled_tools,
                language_hint=None,
                user_id=message.author.id,
                channel_id=channel.id,
                bot=self.bot,
            )
        except Exception as e:
            log.exception("AI support error: %s", e)
            if in_ticket:
                await channel.send(
                    "I ran into an error while processing this. A staff member will take a look."
                )
            return

        docs_similarity = _extract_docs_similarity(result["tool_results"])
        if docs_similarity is not None:
            if in_ticket and docs_similarity < TICKET_MIN_SIMILARITY:
                await channel.send(
                    "I could not find a confident answer in the docs yet. "
                    "A staff member should step in for this ticket."
                )
                return
            if in_general_ai and docs_similarity < GENERAL_MIN_SIMILARITY:
                return

        reply_text = result["reply"]
        if reply_text and reply_text != "(no reply)":
            await channel.send(reply_text)
        elif in_ticket:
            await channel.send(
                "I could not confidently answer this from docs. A staff member should review it."
            )

        if in_ticket and result["suggested_close"]:
            await channel.send(
                "*(AI note: I think this ticket can be closed now. A staff member should confirm.)*"
            )


def setup(bot: discord.Bot):
    bot.add_cog(AITicketsCog(bot))
