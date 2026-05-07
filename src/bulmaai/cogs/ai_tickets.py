import asyncio
import logging
from collections import defaultdict
from typing import Any

import discord
from discord.ext import commands
from openai import AsyncOpenAI

from bulmaai.config import load_settings
from bulmaai.services.openai_client import (
    ConversationMessage,
    is_transient_ai_error,
    run_support_agent,
)
from bulmaai.services.ticket_knowledge import sync_closed_ticket_knowledge
from bulmaai.utils.permissions import can_use_ai_support, is_staff

log = logging.getLogger(__name__)
vision_client = AsyncOpenAI(api_key=load_settings().openai_key)

TICKET_MIN_SCORE = 0.52
LOG_ATTACHMENT_EXTENSIONS = (".log", ".txt")
IMAGE_ATTACHMENT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
DISCORD_MESSAGE_LIMIT = 1900


def _chunk_discord_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be positive")

    remaining = text
    chunks: list[str] = []
    while len(remaining) > limit:
        split_at = max(
            remaining.rfind("\n", 0, limit + 1),
            remaining.rfind(" ", 0, limit + 1),
        )
        if split_at < max(1, limit // 2):
            split_at = limit
        else:
            split_at += 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    if remaining:
        chunks.append(remaining)
    return chunks


def _is_ticket_channel(
    channel: discord.abc.GuildChannel,
    *,
    settings,
) -> bool:
    return (
        isinstance(channel, discord.TextChannel)
        and channel.category
        and settings.ai_ticket_category_id is not None
        and channel.category.id == settings.ai_ticket_category_id
    )


def _is_pinging_bot(message: discord.Message, bot_user: discord.ClientUser | None) -> bool:
    return bot_user is not None and bot_user in message.mentions


def _strip_bot_mentions(text: str, bot_user: discord.ClientUser | None) -> str:
    stripped = text.strip()
    if bot_user is None:
        return stripped

    mention_tokens = {
        bot_user.mention,
        f"<@{bot_user.id}>",
        f"<@!{bot_user.id}>",
    }
    for token in mention_tokens:
        stripped = stripped.replace(token, " ")
    return " ".join(stripped.split())


def _has_support_request_content(
    message: discord.Message,
    bot_user: discord.ClientUser | None,
) -> bool:
    text = _strip_bot_mentions(message.content, bot_user)
    if len(text.strip()) >= 3:
        return True
    return any(_is_image_attachment(attachment) for attachment in message.attachments)


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
        if entry.get("name") != "docs_search":
            continue
        output = entry.get("output")
        if isinstance(output, dict):
            return output
    return None


def _has_user_visible_tool_result(tool_results: list[Any]) -> bool:
    for entry in tool_results:
        output = entry.get("output")
        if isinstance(output, dict) and output.get("suppress_ai_reply") is True:
            return True
    return False


def _build_suggestion_reply(language: str, docs_output: dict[str, Any]) -> str | None:
    suggestions = docs_output.get("suggested_answers") or []
    if not suggestions:
        return None

    labels = {
        "en": "Possible answers from the docs",
        "es": "Posibles respuestas segun la documentacion",
        "pt": "Possiveis respostas segundo a documentacao",
    }
    lines = [f"**{labels.get(language, labels['en'])}:**"]
    for suggestion in suggestions[:3]:
        title = suggestion.get("title") or "Doc match"
        answer = suggestion.get("answer") or ""
        lines.append(f"- **{title}**: {answer}")
    return "\n".join(lines)


def _pending_key(message: discord.Message, *, in_ticket: bool) -> tuple[int, int]:
    return message.channel.id, 0 if in_ticket else message.author.id


def _support_debounce_seconds(settings: Any) -> float:
    return max(float(getattr(settings, "ai_support_debounce_seconds", 0) or 0), 0.0)


def _is_staff_ticket_message(
    message: discord.Message,
    *,
    in_ticket: bool,
    settings: Any,
) -> bool:
    return in_ticket and not getattr(message.author, "bot", False) and is_staff(
        message.author, settings=settings
    )


def _message_content(message: discord.Message) -> str:
    content = message.clean_content.strip()
    attachment_lines = [f"[Attachment] {attachment.filename}" for attachment in message.attachments]
    if attachment_lines:
        content = f"{content}\n" if content else ""
        content += "\n".join(attachment_lines)
    return content.strip()


class AITicketsCog(commands.Cog):
    """AI triage / support for ticket channels and role-authorized support requests."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._channel_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._escalated_ticket_channels: set[int] = set()
        self._ticket_knowledge_task: asyncio.Task[None] | None = None

    def cog_unload(self) -> None:
        for pending_key in list(self._pending_tasks):
            self._cancel_pending_task(pending_key)
        if self._ticket_knowledge_task is not None and not self._ticket_knowledge_task.done():
            self._ticket_knowledge_task.cancel()

    def _cancel_pending_task(self, pending_key: tuple[int, int]) -> None:
        task = self._pending_tasks.pop(pending_key, None)
        if task is None:
            return
        if not task.done():
            task.cancel()

    def _cancel_pending_task_for_message(
        self,
        message: discord.Message,
        *,
        in_ticket: bool,
    ) -> None:
        self._cancel_pending_task(_pending_key(message, in_ticket=in_ticket))

    def _mark_ticket_escalated(self, channel_id: int) -> None:
        self._escalated_ticket_channels.add(channel_id)
        self._cancel_pending_task((channel_id, 0))

    async def _resolve_member_for_user(self, user: discord.abc.User) -> discord.Member | None:
        for guild in self.bot.guilds:
            member = guild.get_member(user.id)
            if member is not None:
                return member
        for guild in self.bot.guilds:
            try:
                return await guild.fetch_member(user.id)
            except discord.NotFound:
                continue
            except discord.HTTPException:
                log.exception(
                    "Failed to fetch DM support member",
                    extra={"event": "dm_support_member_fetch_failed", "guild_id": guild.id, "user_id": user.id},
                )
        return None

    async def _can_use_support_from_message(self, message: discord.Message, *, settings) -> bool:
        if isinstance(message.author, discord.Member):
            return can_use_ai_support(message.author, settings=settings)
        member = await self._resolve_member_for_user(message.author)
        return member is not None and can_use_ai_support(member, settings=settings)

    async def _closed_ticket_sync_loop(self) -> None:
        await self.bot.wait_until_ready()
        interval_seconds = 1800

        while True:
            try:
                summary = await sync_closed_ticket_knowledge(self.bot)
                if summary["scanned"]:
                    log.info(
                        "ticket_knowledge sync scanned=%s indexed=%s skipped=%s",
                        summary["scanned"],
                        summary["indexed"],
                        summary["skipped"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Closed ticket sync failed")

            await asyncio.sleep(interval_seconds)

    async def _send_messages_with_typing(
        self,
        channel: discord.TextChannel,
        messages: list[str],
    ) -> bool:
        chunks = [
            chunk
            for message in messages
            if message
            for chunk in _chunk_discord_message(message)
            if chunk.strip()
        ]
        if not chunks:
            return True

        typing_lead_seconds = max(self.bot.settings.ai_support_typing_lead_seconds, 0)
        allowed_mentions = discord.AllowedMentions.none()
        try:
            if typing_lead_seconds <= 0:
                for chunk in chunks:
                    await channel.send(chunk, allowed_mentions=allowed_mentions)
                return True

            async with channel.typing():
                await asyncio.sleep(typing_lead_seconds)
                for chunk in chunks:
                    await channel.send(chunk, allowed_mentions=allowed_mentions)
            return True
        except discord.HTTPException:
            log.exception(
                "Failed to send AI support response",
                extra={
                    "event": "ai_support_send_failed",
                    "channel_id": getattr(channel, "id", None),
                    "chunk_count": len(chunks),
                    "max_chunk_length": max((len(chunk) for chunk in chunks), default=0),
                },
            )
            return False

    def _serialize_message(
        self,
        message: discord.Message,
        *,
        requester_id: int | None,
    ) -> ConversationMessage | None:
        if message.author.bot and message.author != self.bot.user:
            return None

        content = _message_content(message)
        if not content:
            return None

        if message.author == self.bot.user:
            speaker_kind = "assistant"
            role = "assistant"
        elif requester_id is not None and message.author.id == requester_id:
            speaker_kind = "requester"
            role = "user"
        elif isinstance(message.author, discord.Member) and is_staff(message.author):
            speaker_kind = "staff"
            role = "user"
        else:
            speaker_kind = "participant"
            role = "user"

        speaker_name = getattr(message.author, "display_name", message.author.name)
        return ConversationMessage(
            role=role,
            content=content,
            speaker_name=speaker_name,
            speaker_id=str(message.author.id),
            speaker_kind=speaker_kind,
        )

    async def _build_ticket_history(self, message: discord.Message) -> list[ConversationMessage]:
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return []

        messages = [
            entry
            async for entry in channel.history(
                limit=self.bot.settings.ai_support_history_limit,
                oldest_first=True,
            )
        ]
        requester_id = message.author.id

        history: list[ConversationMessage] = []
        for entry in messages:
            serialized = self._serialize_message(entry, requester_id=requester_id)
            if serialized is not None:
                history.append(serialized)
        return history

    async def _build_general_history(self, message: discord.Message) -> list[ConversationMessage]:
        channel = message.channel
        if not hasattr(channel, "history"):
            return []

        relevant_messages: list[discord.Message] = []
        author_id = message.author.id

        async for entry in channel.history(limit=40):
            if entry.author.bot and entry.author != self.bot.user:
                continue
            if entry.author == self.bot.user or entry.author.id == author_id:
                relevant_messages.append(entry)
                continue
            break

        relevant_messages.reverse()

        history: list[ConversationMessage] = []
        for entry in relevant_messages:
            serialized = self._serialize_message(entry, requester_id=author_id)
            if serialized is not None:
                history.append(serialized)
        return history

    async def _build_history(self, message: discord.Message, *, in_ticket: bool) -> list[ConversationMessage]:
        if in_ticket:
            return await self._build_ticket_history(message)
        return await self._build_general_history(message)

    async def _extract_image_context(self, message: discord.Message) -> str:
        image_urls = [attachment.url for attachment in message.attachments if _is_image_attachment(attachment)]
        if not image_urls:
            return ""

        snippets = await asyncio.gather(
            *(self._extract_single_image_context(url) for url in image_urls[:2])
        )
        return "\n".join(snippet for snippet in snippets if snippet)[:2000]

    async def _extract_single_image_context(self, url: str) -> str:
        settings = self.bot.settings
        try:
            payload: Any = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Read this support screenshot and extract only actionable "
                                "details relevant to support: errors, warnings, version hints, "
                                "symptoms, access or whitelist requests, visible roles or status, "
                                "buttons clicked, and any text that changes the recommended next step."
                            ),
                        },
                        {"type": "input_image", "image_url": url},
                    ],
                }
            ]
            response = await asyncio.wait_for(
                vision_client.responses.create(
                    model=settings.openai_vision_model,
                    input=payload,
                    max_output_tokens=settings.openai_support_max_output_tokens,
                    text={"verbosity": "medium"},
                ),
                timeout=settings.ai_support_timeout_seconds,
            )
            if response.output_text:
                return response.output_text.strip()
        except Exception:
            log.exception("Failed to extract image context from %s", url)
        return ""

    async def _process_support_message(self, message: discord.Message) -> None:
        channel = message.channel
        if not hasattr(channel, "send"):
            return

        settings = self.bot.settings
        in_ticket = isinstance(channel, discord.TextChannel) and _is_ticket_channel(channel, settings=settings)
        in_dm = isinstance(channel, discord.DMChannel)
        bot_pinged = _is_pinging_bot(message, self.bot.user)
        mention_request = bot_pinged and _has_support_request_content(message, self.bot.user)
        dm_request = in_dm and _has_support_request_content(message, self.bot.user)
        author_has_support_access = await self._can_use_support_from_message(message, settings=settings)

        if not in_ticket and not mention_request and not dm_request:
            return
        if (mention_request or dm_request) and not in_ticket and not author_has_support_access:
            return

        if in_ticket and channel.id in self._escalated_ticket_channels:
            return

        async with self._channel_locks[channel.id]:
            try:
                history = await self._build_history(message, in_ticket=in_ticket)
                image_context = await self._extract_image_context(message)
                if image_context:
                    history.append(
                        ConversationMessage(
                            role="user",
                            content=f"[Image context extracted from attachment]\n{image_context}",
                            speaker_name=getattr(message.author, "display_name", message.author.name),
                            speaker_id=str(message.author.id),
                            speaker_kind="requester",
                        )
                    )

                enabled_tools = ["docs_search", "start_patreon_whitelist_flow"]
                result = await run_support_agent(
                    messages=history,
                    enabled_tools=enabled_tools,
                    language_hint=None,
                    model_override=(
                        settings.openai_support_model if in_ticket else settings.openai_model
                    ),
                    use_cache=in_ticket,
                    user_id=message.author.id,
                    channel_id=channel.id,
                    bot=self.bot,
                    settings=settings,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                transient = is_transient_ai_error(error)
                log.exception(
                    "AI support error: %s",
                    error,
                    extra={
                        "event": "ai_support_error",
                        "guild_id": getattr(message.guild, "id", None),
                        "channel_id": channel.id,
                        "message_id": message.id,
                        "user_id": message.author.id,
                        "is_ticket": in_ticket,
                        "transient": transient,
                    },
                )
                if in_ticket:
                    if transient:
                        await self._send_messages_with_typing(
                            channel,
                            [
                                "I ran into a temporary AI service issue while processing this. (A.I. API Outage) "
                                "Please try again in a moment."
                            ],
                        )
                    elif await self._send_messages_with_typing(
                        channel,
                        ["I ran into an error while processing this. A staff member should take a look."],
                    ):
                        self._mark_ticket_escalated(channel.id)
                elif mention_request:
                    await self._send_messages_with_typing(
                        channel,
                        [
                            f"{message.author.mention} I ran into an error while processing that. "
                            "Please try again or open a support ticket."
                        ],
                    )
                return

            if _has_user_visible_tool_result(result["tool_results"]):
                return

            docs_output = _extract_docs_output(result["tool_results"])
            docs_score = float(docs_output.get("best_score", 0.0)) if docs_output else None
            outgoing_messages: list[str] = []

            if docs_score is not None and in_ticket and docs_score < TICKET_MIN_SCORE:
                suggestion_reply = _build_suggestion_reply(result["language"], docs_output)
                if suggestion_reply:
                    outgoing_messages.append(suggestion_reply)
                outgoing_messages.append(
                    "I could not find a confident enough answer yet. A staff member should review this ticket."
                )
                if await self._send_messages_with_typing(channel, outgoing_messages):
                    self._mark_ticket_escalated(channel.id)
                return

            reply_text = result["reply"].strip()
            if reply_text and reply_text != "(no reply)":
                outgoing_messages.append(reply_text)
            else:
                suggestion_reply = _build_suggestion_reply(result["language"], docs_output or {})
                if suggestion_reply and (in_ticket or mention_request):
                    outgoing_messages.append(suggestion_reply)
                elif in_ticket:
                    outgoing_messages.append(
                        "I could not confidently answer this from the docs. A staff member should review it."
                    )
                    should_mark_escalated = True
                elif mention_request:
                    outgoing_messages.append(
                        "I couldn't find a confident docs-backed answer for that. Please open a ticket if it needs follow-up."
                    )
                    should_mark_escalated = False
            if reply_text and reply_text != "(no reply)":
                should_mark_escalated = False
            elif suggestion_reply and (in_ticket or mention_request):
                should_mark_escalated = False

            if in_ticket and result["suggested_close"]:
                outgoing_messages.append(
                    "*(Support note: this looks solved based on the docs, but a staff member should confirm before closing.)*"
                )

            if await self._send_messages_with_typing(channel, outgoing_messages) and should_mark_escalated:
                self._mark_ticket_escalated(channel.id)

    async def _process_message_after_debounce(
        self,
        message: discord.Message,
        *,
        pending_key: tuple[int, int],
    ) -> None:
        channel = message.channel
        if getattr(channel, "id", None) is None:
            return

        try:
            debounce_seconds = _support_debounce_seconds(self.bot.settings)
            if debounce_seconds:
                await asyncio.sleep(debounce_seconds)
            await self._process_support_message(message)
        except asyncio.CancelledError:
            return
        finally:
            current_task = asyncio.current_task()
            if self._pending_tasks.get(pending_key) is current_task:
                self._pending_tasks.pop(pending_key, None)

    def _schedule_support_response(self, message: discord.Message, *, in_ticket: bool) -> None:
        pending_key = _pending_key(message, in_ticket=in_ticket)
        self._cancel_pending_task(pending_key)
        task = asyncio.create_task(
            self._process_message_after_debounce(message, pending_key=pending_key)
        )
        self._pending_tasks[pending_key] = task

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.bot.settings.ai_closed_ticket_category_ids:
            return
        if self._ticket_knowledge_task is not None and not self._ticket_knowledge_task.done():
            return
        self._ticket_knowledge_task = asyncio.create_task(self._closed_ticket_sync_loop())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        settings = self.bot.settings
        if not settings.ai_support_enabled:
            return

        channel = message.channel
        if isinstance(channel, discord.DMChannel):
            if message.author.bot:
                return
            if message.content.startswith(("!", "/", ".")):
                return
            if _contains_log_attachment(message):
                return
            if not await self._can_use_support_from_message(message, settings=settings):
                return
            self._schedule_support_response(message, in_ticket=False)
            return

        if not message.guild or not isinstance(channel, discord.TextChannel):
            return

        in_ticket = _is_ticket_channel(channel, settings=settings)
        bot_pinged = _is_pinging_bot(message, self.bot.user)
        mention_request = bot_pinged and _has_support_request_content(message, self.bot.user)
        author_has_support_access = await self._can_use_support_from_message(message, settings=settings)
        if not in_ticket and not mention_request:
            return
        if mention_request and not in_ticket and not author_has_support_access:
            return

        if not message.author.bot and isinstance(message.author, discord.Member):
            self._cancel_pending_task_for_message(message, in_ticket=in_ticket)

        if message.author.bot or not isinstance(message.author, discord.Member):
            return
        if _is_staff_ticket_message(message, in_ticket=in_ticket, settings=settings):
            return
        if in_ticket and channel.id in self._escalated_ticket_channels:
            return
        if message.content.startswith(("!", "/", ".")) and not mention_request:
            return
        if _contains_log_attachment(message):
            return

        self._schedule_support_response(message, in_ticket=in_ticket)


def setup(bot: discord.Bot):
    bot.add_cog(AITicketsCog(bot))
