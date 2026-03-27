import asyncio
import logging
from collections import defaultdict
from typing import Any

import discord
from discord.ext import commands
from openai import AsyncOpenAI

from bulmaai.config import load_settings
from bulmaai.services.openai_client import run_support_agent
from bulmaai.utils.permissions import is_staff, is_bruno

log = logging.getLogger(__name__)
settings = load_settings()
vision_client = AsyncOpenAI(api_key=settings.openai_key)

GENERAL_MIN_SIMILARITY = 0.70
TICKET_MIN_SIMILARITY = 0.45
LOG_ATTACHMENT_EXTENSIONS = (".log", ".txt")
IMAGE_ATTACHMENT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def _is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    return (
        isinstance(channel, discord.TextChannel)
        and channel.category
        and settings.ai_ticket_category_id is not None
        and channel.category.id == settings.ai_ticket_category_id
    )


def _is_general_ai_channel(channel: discord.TextChannel) -> bool:
    return channel.id in set(settings.ai_general_channel_ids)


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
    return any(trigger in lowered for trigger in triggers)


def _contains_log_attachment(message: discord.Message) -> bool:
    return any(
        attachment.filename.lower().endswith(LOG_ATTACHMENT_EXTENSIONS)
        for attachment in message.attachments
    )


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_ATTACHMENT_EXTENSIONS)


def _extract_docs_output(tool_results: list[Any]) -> dict[str, Any] | None:
    for entry in tool_results:
        if entry.get("name") == "docs_search":
            output = entry.get("output")
            if isinstance(output, dict):
                return output
    return None


def _build_suggestion_reply(language: str, docs_output: dict[str, Any]) -> str | None:
    suggestions = docs_output.get("suggested_answers") or []
    if not suggestions:
        return None

    labels = {
        "en": "Possible answers from the docs",
        "es": "Posibles respuestas según la documentación",
        "pt": "Possíveis respostas segundo a documentação",
    }
    lines = [f"**{labels.get(language, labels['en'])}:**"]
    for suggestion in suggestions[:3]:
        title = suggestion.get("title") or "Doc match"
        answer = suggestion.get("answer") or ""
        lines.append(f"• **{title}**: {answer}")
    return "\n".join(lines)


class AITicketsCog(commands.Cog):
    """AI triage / support for ticket and configured AI channels."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._channel_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _build_history(self, channel: discord.TextChannel, limit: int | None = None) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        async for msg in channel.history(
            limit=limit or settings.ai_support_history_limit,
            oldest_first=True,
        ):
            content = msg.content.strip()
            if not content and msg.attachments:
                content = "\n".join(f"[Attachment] {attachment.filename}" for attachment in msg.attachments)
            if not content:
                continue
            role = "assistant" if msg.author == self.bot.user else "user"
            history.append({"role": role, "content": content})
        return history

    async def _extract_ticket_image_context(self, message: discord.Message) -> str:
        image_urls = [attachment.url for attachment in message.attachments if _is_image_attachment(attachment)]
        if not image_urls:
            return ""

        snippets: list[str] = []
        for url in image_urls[:2]:
            try:
                payload: Any = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Read this support screenshot and extract only actionable "
                                    "details: errors, warnings, version hints, symptoms, and buttons clicked."
                                ),
                            },
                            {"type": "input_image", "image_url": url},
                        ],
                    }
                ]
                response = await vision_client.responses.create(
                    model=settings.openai_vision_model,
                    input=payload,
                    text={"verbosity": "low"},
                )
                if response.output_text:
                    snippets.append(response.output_text.strip())
            except Exception:
                log.exception("Failed to extract image context from %s", url)

        return "\n".join(snippets)[:2000]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not settings.ai_support_enabled:
            return
        if message.author.bot or not message.guild:
            return
        if is_bruno(message.author):
            pass
            if is_staff(message.author):
                return
        if not isinstance(message.author, discord.Member):
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
        if _contains_log_attachment(message):
            return

        async with self._channel_locks[channel.id]:
            try:
                async with channel.typing():
                    history = await self._build_history(channel)
                    if in_ticket:
                        image_context = await self._extract_ticket_image_context(message)
                        if image_context:
                            history.append(
                                {
                                    "role": "user",
                                    "content": f"[Image context extracted from attachment]\n{image_context}",
                                }
                            )

                    enabled_tools = [
                        "start_patreon_whitelist_flow" if _wants_whitelist_flow(message.content) else "docs_search"
                    ]
                    result = await run_support_agent(
                        messages=history,
                        enabled_tools=enabled_tools,
                        language_hint=None,
                        user_id=message.author.id,
                        channel_id=channel.id,
                        bot=self.bot,
                    )
            except Exception as error:
                log.exception("AI support error: %s", error)
                if in_ticket:
                    await channel.send(
                        "I ran into an error while processing this. A staff member should take a look."
                    )
                return

        docs_output = _extract_docs_output(result["tool_results"])
        docs_similarity = float(docs_output.get("best_similarity", 0.0)) if docs_output else None

        if docs_similarity is not None:
            if in_ticket and docs_similarity < TICKET_MIN_SIMILARITY:
                suggestion_reply = _build_suggestion_reply(result["language"], docs_output)
                if suggestion_reply:
                    await channel.send(suggestion_reply)
                await channel.send(
                    "I could not find a fully confident answer in the docs yet. A staff member should review this ticket."
                )
                return
            if in_general_ai and docs_similarity < GENERAL_MIN_SIMILARITY:
                suggestion_reply = _build_suggestion_reply(result["language"], docs_output)
                if suggestion_reply:
                    await channel.send(suggestion_reply)
                return

        reply_text = result["reply"]
        if reply_text and reply_text != "(no reply)":
            await channel.send(reply_text)
        else:
            suggestion_reply = _build_suggestion_reply(result["language"], docs_output or {})
            if suggestion_reply:
                await channel.send(suggestion_reply)
            elif in_ticket:
                await channel.send(
                    "I could not confidently answer this from the docs. A staff member should review it."
                )

        if in_ticket and result["suggested_close"]:
            await channel.send(
                "*(AI note: this looks solved based on the docs, but a staff member should confirm before closing.)*"
            )


def setup(bot: discord.Bot):
    bot.add_cog(AITicketsCog(bot))
