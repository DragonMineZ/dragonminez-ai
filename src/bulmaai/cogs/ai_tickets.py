import asyncio
import logging
import random
import re
import time
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands
from openai import AsyncOpenAI

from bulmaai.config import load_settings
from bulmaai.services import ai_budget
from bulmaai.services.ai_guard import can_post_publicly, defuse_mentions, safe_name, strip_links
from bulmaai.services.ai_tools import SUPPORT_TOOL_NAMES
from bulmaai.services.moderation import ModerationState
from bulmaai.services.openai_client import (
    ConversationMessage,
    is_transient_ai_error,
    run_support_agent,
)
from bulmaai.services.support_intent import (
    SUPPORT_INTENT_UNCLEAR,
    SupportIntent,
    classify_support_intent,
)
from bulmaai.services.ticket_ai_state import (
    get_ai_disabled_ticket_channels,
    set_ticket_ai_disabled,
)
from bulmaai.services.ticket_pages import StoredPage, page_url
from bulmaai.services.ticket_transcripts import (
    TicketSummary,
    TranscriptLine,
    delete_image_analyses,
    get_image_analyses,
    record_ticket_transcript,
    render_knowledge_markdown,
    render_transcript_text,
    save_image_analysis,
    summarize_ticket,
    ticket_knowledge_filename,
    upload_ticket_knowledge,
)
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import (
    can_use_ai_support,
    has_any_allowed_role,
    has_patreon_access_role,
    is_allowed_guild_id,
    is_staff,
)

log = logging.getLogger(__name__)
vision_client = AsyncOpenAI(api_key=load_settings().openai_key)

LOG_ATTACHMENT_EXTENSIONS = (".log", ".txt")
IMAGE_ATTACHMENT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
DISCORD_MESSAGE_LIMIT = 1900
MAX_IMAGE_ANALYSIS_CHARS = 1000
# ponytail: hard cap on transcript size; page through history if tickets ever exceed it.
TRANSCRIPT_MESSAGE_LIMIT = 1000
RESOLVE_BUTTON_PREFIX = "ticket_resolved:"
# ponytail: legacy Ticket Tool tickets still work; new ones come from cogs/tickets.py. Drop this once Ticket Tool is gone.
TICKET_TOOL_BOT_ID = 557628352828014614
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ACK_MESSAGE_RE = re.compile(
    r"(?i)^\W*(thanks?|thank you|thx|ty|ok|okay|k|kk|cool|nice|great|perfect|got it|"
    r"gracias|vale|listo|perfecto|dale|obrigad[oa]|valeu|beleza)\W*$"
)
# ponytail: public-channel context window; widen if pings keep missing earlier chatter.
GENERAL_CONTEXT_MESSAGE_LIMIT = 15
# ponytail: in-memory per-user window (a restart resets it); stops one member draining the shared daily AI budget.
SUPPORT_RATE_LIMIT_CALLS = 8
SUPPORT_RATE_LIMIT_WINDOW_SECONDS = 600
PUBLIC_FALLBACK_TEXT = "I can't answer that one here. Try `/ask`, or open a ticket."
RATE_LIMITED_TEXT = (
    "You're sending messages faster than I can keep up, so I'll pause for a few minutes. Staff can still see this."
)
GENERAL_CONTEXT_MAX_AGE = timedelta(minutes=30)
REPLY_PREVIEW_CHARS = 80
AI_PAUSED_TEXT = (
    "My AI circuits are recharging for today ⚡ I'll be back at 00:00 UTC. "
    "A staff member will help you in the meantime."
)
RESOLVE_PROMPT_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "question": "Have I solved your problem?",
        "yes": "Yes, it's solved",
        "no": "No, I still need help",
        "thanks": "Glad I could help! 🎉 Thanks for reaching out, this ticket will be closed now.",
        "escalated": "Sorry about that! I've flagged this ticket for staff, a team member will take it from here.",
    },
    "es": {
        "question": "¿He resuelto tu problema?",
        "yes": "Sí, está resuelto",
        "no": "No, sigo necesitando ayuda",
        "thanks": "¡Me alegra haber ayudado! 🎉 Gracias por escribirnos, este ticket se cerrará ahora.",
        "escalated": "¡Lo siento! He avisado al staff, un miembro del equipo continuará desde aquí.",
    },
    "pt": {
        "question": "Resolvi o seu problema?",
        "yes": "Sim, está resolvido",
        "no": "Não, ainda preciso de ajuda",
        "thanks": "Que bom que pude ajudar! 🎉 Obrigado pelo contato, este ticket será fechado agora.",
        "escalated": "Desculpe! Avisei a equipe, um membro da staff vai continuar daqui.",
    },
}


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
    if bot_user is None:
        return False

    bot_user_id = getattr(bot_user, "id", None)
    for mentioned_user in getattr(message, "mentions", ()) or ():
        if mentioned_user == bot_user or getattr(mentioned_user, "id", None) == bot_user_id:
            return True

    reference = getattr(message, "reference", None)
    if reference is None:
        return False

    for attr in ("resolved", "cached_message"):
        referenced_message = getattr(reference, attr, None)
        referenced_author = getattr(referenced_message, "author", None)
        if referenced_author == bot_user or getattr(referenced_author, "id", None) == bot_user_id:
            return True

    return False


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


def _message_support_intent(
    message: discord.Message,
    bot_user: discord.ClientUser | None,
) -> SupportIntent:
    text = _strip_bot_mentions(message.content, bot_user)
    return classify_support_intent(
        text,
        has_image=any(_is_image_attachment(attachment) for attachment in message.attachments),
    )


def _is_ack_message(text: str) -> bool:
    """Pure thanks/ok replies need no AI answer, even inside tickets."""
    return bool(ACK_MESSAGE_RE.fullmatch(text.strip()))


def _relative_age(created_at: datetime | None, now: datetime | None = None) -> str | None:
    if created_at is None:
        return None
    seconds = max(((now or datetime.now(timezone.utc)) - created_at).total_seconds(), 0)
    if seconds < 60:
        return "just now"
    for unit_seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= unit_seconds:
            return f"{int(seconds // unit_seconds)}{suffix} ago"
    return None


def _is_reply_to_bot(message: discord.Message, bot_user: discord.ClientUser | None) -> bool:
    reference = getattr(message, "reference", None)
    referenced = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
    author = getattr(referenced, "author", None)
    return bot_user is not None and author is not None and getattr(author, "id", None) == bot_user.id


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


def _has_user_visible_tool_result(tool_results: list[Any]) -> bool:
    for entry in tool_results:
        output = entry.get("output")
        if isinstance(output, dict) and output.get("suppress_ai_reply") is True:
            return True
    return False


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


def _message_content(
    message: discord.Message,
    image_analyses: dict[int, str] | None = None,
) -> str:
    """Text plus attachment markers. Images only show as text when analyzed before; never re-processed."""
    content = message.clean_content.strip()
    attachment_lines: list[str] = []
    for attachment in message.attachments:
        analysis = (image_analyses or {}).get(getattr(attachment, "id", None))
        if analysis and _is_image_attachment(attachment):
            attachment_lines.append(f"[Image: {analysis[:MAX_IMAGE_ANALYSIS_CHARS]}]")
        else:
            attachment_lines.append(f"[Attachment] {attachment.filename}")
    if attachment_lines:
        content = f"{content}\n" if content else ""
        content += "\n".join(attachment_lines)
    return content.strip()


def _resolve_prompt_probability(
    confidence: float | None,
    *,
    min_confidence: float,
    exponent: float,
) -> float:
    """Chance to ask "Have I solved your problem?": 0 below the floor, then confidence**exponent,
    so asks ramp up steeply as the model gets surer (exponent 3: 0.6→22%, 0.8→51%, 0.95→86%)."""
    if confidence is None or confidence < min_confidence:
        return 0.0
    return min(1.0, confidence ** max(exponent, 0.0))


def _resolve_text(language: str) -> dict[str, str]:
    return RESOLVE_PROMPT_TEXT.get(language, RESOLVE_PROMPT_TEXT["en"])


def _build_resolve_view(requester_id: int, language: str) -> discord.ui.View:
    """Buttons are handled by the cog's on_interaction via custom_id, so they survive restarts."""
    text = _resolve_text(language)
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=text["yes"],
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"{RESOLVE_BUTTON_PREFIX}yes:{requester_id}:{language}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label=text["no"],
            style=discord.ButtonStyle.secondary,
            emoji="🙋",
            custom_id=f"{RESOLVE_BUTTON_PREFIX}no:{requester_id}:{language}",
        )
    )
    return view


def _parse_resolve_custom_id(custom_id: str) -> tuple[bool, int, str] | None:
    if not custom_id.startswith(RESOLVE_BUTTON_PREFIX):
        return None
    try:
        answer, requester_id, language = custom_id.removeprefix(RESOLVE_BUTTON_PREFIX).split(":")
        return answer == "yes", int(requester_id), language
    except ValueError:
        return None


def _ticket_vector_store_id(settings: Any) -> str | None:
    """Closed tickets land in the support store by default so file_search learns from them."""
    explicit = getattr(settings, "openai_ticket_vector_store_id", None)
    if explicit:
        return explicit
    return next(iter(getattr(settings, "openai_support_vector_store_ids", ()) or ()), None)


def _ticket_tool_closer_id(message: discord.Message) -> int | None | bool:
    """Ticket Tool announces closes with a "Ticket Closed by @user" embed.
    Returns the closer's id, None when closed but no mention, False when not a close."""
    if getattr(message.author, "id", None) != TICKET_TOOL_BOT_ID:
        return False
    for embed in message.embeds:
        text = f"{embed.title or ''}\n{embed.description or ''}"
        if "closed by" in text.lower():
            mention = USER_MENTION_RE.search(text)
            return int(mention.group(1)) if mention else None
    return False


def _build_close_embed(
    *,
    channel_name: str,
    requester_id: int | None,
    closed_by_id: int | None,
    resolved: bool,
    summary: TicketSummary | None,
    confidence: float | None,
    message_count: int,
    open_for: timedelta,
    added_to_knowledge: bool,
    closed_at: datetime,
    page_link: str | None = None,
    page_expires_at: datetime | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎫 {summary.title if summary else channel_name}"[:256],
        color=discord.Color.green() if resolved else discord.Color.orange(),
        timestamp=closed_at,
    )
    embed.add_field(name="Requester", value=f"<@{requester_id}>" if requester_id else "Unknown")
    embed.add_field(name="Closed by", value=f"<@{closed_by_id}>" if closed_by_id else "Unknown")
    embed.add_field(name="Outcome", value="✅ Solved" if resolved else "🟠 Unresolved")
    embed.add_field(name="Messages", value=str(message_count))
    embed.add_field(name="AI confidence", value=f"{confidence:.0%}" if confidence is not None else "n/a")
    embed.add_field(name="Open for", value=str(open_for).split(".")[0])
    if page_link:
        keep = f"expires <t:{int(page_expires_at.timestamp())}:R>" if page_expires_at else "kept permanently"
        embed.add_field(name="Web transcript", value=f"[Open the HTML transcript]({page_link}) · {keep}", inline=False)
    if summary is not None:
        embed.add_field(name="Problem", value=(summary.problem or "-")[:1024], inline=False)
        embed.add_field(name="Resolution", value=(summary.resolution or "-")[:1024], inline=False)
        if summary.tags:
            embed.add_field(name="Tags", value=" ".join(f"`{tag}`" for tag in summary.tags), inline=False)
    embed.set_footer(
        text=f"#{channel_name} · "
        + ("📚 Added to AI knowledge" if added_to_knowledge else "Not added to AI knowledge")
    )
    return embed


class AITicketsCog(ReloadableCog):
    """AI triage / support for ticket channels and role-authorized support requests."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._channel_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._escalated_ticket_channels: set[int] = set()
        self._resolve_prompts: dict[int, discord.Message] = {}
        # ponytail: in-memory, a restart just shows "n/a" confidence on the close embed.
        self._last_confidence: dict[int, float] = {}
        self._closing_channels: set[int] = set()
        # ponytail: in-memory dedupe; reopening a ticket clears its entry (forget_archived), a restart clears all.
        self._archived_channels: set[int] = set()
        self._paused_notices: dict[int, date] = {}
        self._rate_limit_state = ModerationState()
        self._support_events: dict[tuple[int, int], list[float]] = defaultdict(list)
        # ponytail: one entry per ticket channel answered; tiny, cleared on restart.
        self._ticket_owners: dict[int, int | None] = {}

    async def on_startup(self) -> None:
        try:
            self._escalated_ticket_channels |= await get_ai_disabled_ticket_channels()
        except Exception:
            log.exception("Failed to load persisted AI ticket disabled channels")

    async def on_shutdown(self) -> None:
        for pending_key in list(self._pending_tasks):
            self._cancel_pending_task(pending_key)

    def is_busy(self) -> bool:
        return any(not task.done() for task in self._pending_tasks.values())

    def export_state(self) -> dict[str, Any]:
        return {
            "escalated": set(self._escalated_ticket_channels),
            "resolve_prompts": dict(self._resolve_prompts),
            "last_confidence": dict(self._last_confidence),
            "archived": set(self._archived_channels),
            "paused_notices": dict(self._paused_notices),
            "ticket_owners": dict(self._ticket_owners),
        }

    def import_state(self, state: dict[str, Any]) -> None:
        self._escalated_ticket_channels |= state.get("escalated", set())
        self._resolve_prompts.update(state.get("resolve_prompts", {}))
        self._last_confidence.update(state.get("last_confidence", {}))
        self._archived_channels |= state.get("archived", set())
        self._paused_notices.update(state.get("paused_notices", {}))
        self._ticket_owners.update(state.get("ticket_owners", {}))

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

    async def _mark_ticket_escalated(self, channel_id: int) -> None:
        self._escalated_ticket_channels.add(channel_id)
        self._cancel_pending_task((channel_id, 0))
        try:
            await set_ticket_ai_disabled(channel_id, True)
        except Exception:
            log.exception("Failed to persist AI ticket disabled state for channel %s", channel_id)

    def is_ticket_ai_enabled(self, channel_id: int) -> bool:
        return channel_id not in self._escalated_ticket_channels

    async def set_ticket_ai_enabled(self, channel_id: int, enabled: bool) -> None:
        """Shared by /aisupport and the admin panel."""
        if not enabled:
            await self._mark_ticket_escalated(channel_id)
            return
        self._escalated_ticket_channels.discard(channel_id)
        try:
            await set_ticket_ai_disabled(channel_id, False)
        except Exception:
            log.exception("Failed to persist AI ticket enabled state for channel %s", channel_id)

    @discord.slash_command(name="aisupport", description="Toggle AI support on or off in this ticket channel.")
    async def aisupport(self, ctx: discord.ApplicationContext):
        settings = self.bot.settings
        channel = ctx.channel
        if not isinstance(ctx.author, discord.Member) or not is_staff(ctx.author, settings=settings):
            return await ctx.respond("Only staff can toggle AI support.", ephemeral=True)
        if not _is_ticket_channel(channel, settings=settings):
            return await ctx.respond("This isn't an AI support ticket channel.", ephemeral=True)

        enable = not self.is_ticket_ai_enabled(channel.id)
        await self.set_ticket_ai_enabled(channel.id, enable)
        await ctx.respond(f"AI support is now **{'on' if enable else 'off'}** in this channel.")

    async def _resolve_member_for_user(self, user: discord.abc.User) -> discord.Member | None:
        guilds = [g for g in self.bot.guilds if is_allowed_guild_id(g.id, self.bot.settings)]
        for guild in guilds:
            member = guild.get_member(user.id)
            if member is not None:
                return member
        for guild in guilds:
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

    def _speaker_kind(self, message: discord.Message, requester_id: int | None) -> str:
        if message.author == self.bot.user:
            return "assistant"
        if requester_id is not None and message.author.id == requester_id:
            return "requester"
        if isinstance(message.author, discord.Member) and is_staff(message.author, settings=self.bot.settings):
            return "staff"
        return "participant"

    def _serialize_message(
        self,
        message: discord.Message,
        *,
        requester_id: int | None,
        image_analyses: dict[int, str] | None = None,
    ) -> ConversationMessage | None:
        if message.author.bot and message.author != self.bot.user:
            return None

        content = _message_content(message, image_analyses)
        if not content:
            return None

        speaker_kind = self._speaker_kind(message, requester_id)
        speaker_name = getattr(message.author, "display_name", message.author.name)
        serialized = ConversationMessage(
            role="assistant" if speaker_kind == "assistant" else "user",
            content=content,
            speaker_name=speaker_name,
            speaker_id=str(message.author.id),
            speaker_kind=speaker_kind,
        )
        age = _relative_age(getattr(message, "created_at", None))
        if age:
            serialized["age"] = age
        referenced = getattr(getattr(message, "reference", None), "resolved", None)
        if isinstance(referenced, discord.Message):
            preview = " ".join(referenced.clean_content.split())[:REPLY_PREVIEW_CHARS]
            author_name = getattr(referenced.author, "display_name", referenced.author.name)
            serialized["reply_to"] = f'{author_name}: "{preview}"'
        return serialized

    async def _cached_image_analyses(self, messages: list[discord.Message]) -> dict[int, str]:
        attachment_ids = [
            attachment.id
            for entry in messages
            for attachment in entry.attachments
            if _is_image_attachment(attachment) and getattr(attachment, "id", None) is not None
        ]
        if not attachment_ids:
            return {}
        try:
            return await get_image_analyses(attachment_ids)
        except Exception:
            log.exception("Failed to load cached image analyses")
            return {}

    async def _build_ticket_history(
        self,
        message: discord.Message,
        image_analyses: dict[int, str],
    ) -> list[ConversationMessage]:
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return []

        # Newest N, then oldest first: history(oldest_first=True) would return the ticket's FIRST N.
        messages = [
            entry async for entry in channel.history(limit=self.bot.settings.ai_support_history_limit)
        ]
        messages.reverse()
        requester_id = message.author.id
        analyses = {**await self._cached_image_analyses(messages), **image_analyses}

        history: list[ConversationMessage] = []
        for entry in messages:
            serialized = self._serialize_message(entry, requester_id=requester_id, image_analyses=analyses)
            if serialized is not None:
                history.append(serialized)
        return history

    async def _build_general_history(
        self,
        message: discord.Message,
        image_analyses: dict[int, str],
    ) -> list[ConversationMessage]:
        channel = message.channel
        if not hasattr(channel, "history"):
            return []

        author_id = message.author.id
        cutoff = datetime.now(timezone.utc) - GENERAL_CONTEXT_MAX_AGE
        relevant_messages = [
            entry
            async for entry in channel.history(limit=GENERAL_CONTEXT_MESSAGE_LIMIT)
            if entry.id == message.id or entry.created_at >= cutoff
        ]
        relevant_messages.reverse()
        analyses = {**await self._cached_image_analyses(relevant_messages), **image_analyses}

        history: list[ConversationMessage] = []
        for entry in relevant_messages:
            serialized = self._serialize_message(entry, requester_id=author_id, image_analyses=analyses)
            if serialized is not None:
                history.append(serialized)
        return history

    async def _build_history(
        self,
        message: discord.Message,
        *,
        in_ticket: bool,
        image_analyses: dict[int, str] | None = None,
    ) -> list[ConversationMessage]:
        if in_ticket:
            return await self._build_ticket_history(message, image_analyses or {})
        return await self._build_general_history(message, image_analyses or {})

    async def _extract_image_context(self, message: discord.Message) -> dict[int, str]:
        """Analyze up to 2 new screenshots; reuse cached analyses so an image is only processed once."""
        attachments = [attachment for attachment in message.attachments if _is_image_attachment(attachment)][:2]
        if not attachments:
            return {}

        analyses = await self._cached_image_analyses([message])
        missing = [attachment for attachment in attachments if attachment.id not in analyses]
        fresh = await asyncio.gather(
            *(self._extract_single_image_context(attachment.url) for attachment in missing)
        )
        for attachment, analysis in zip(missing, fresh):
            if not analysis:
                continue
            analyses[attachment.id] = analysis
            try:
                await save_image_analysis(
                    attachment_id=attachment.id,
                    channel_id=message.channel.id,
                    analysis=analysis,
                )
            except Exception:
                log.exception("Failed to cache image analysis for attachment %s", attachment.id)
        return {attachment.id: analyses[attachment.id] for attachment in attachments if attachment.id in analyses}

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
                                "Read this support screenshot. Start with one short sentence saying "
                                "what it shows, then extract only actionable details relevant to "
                                "support: errors, warnings, version hints, symptoms, visible roles "
                                "or status, buttons clicked, and any text that changes the "
                                "recommended next step. Be concise; this text replaces the image "
                                "in the saved ticket transcript."
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
            ai_budget.record_response(response)
            if response.output_text:
                return response.output_text.strip()
        except Exception:
            log.exception("Failed to extract image context from %s", url)
        return ""

    def _support_context_lines(
        self,
        message: discord.Message,
        *,
        member: discord.Member | None,
        in_ticket: bool,
        in_dm: bool,
    ) -> list[str]:
        settings = self.bot.settings
        channel = message.channel
        if in_ticket:
            where = "support ticket"
        elif in_dm:
            where = "direct message"
        else:
            where = f"public channel #{getattr(channel, 'name', '?')} (you were pinged; answer only the requester)"
        roles: list[str] = []
        if member is not None:
            if is_staff(member, settings=settings):
                roles.append("staff")
            if has_patreon_access_role(member, settings=settings):
                roles.append("patron")
            if has_any_allowed_role(member, settings.dev_jar_tester_role_ids):
                roles.append("tester")
        name = safe_name(getattr(message.author, "display_name", message.author.name))
        lines = [f"channel: {where}", f"requester: {name} (roles: {', '.join(roles) or 'member'})"]
        opened = _relative_age(getattr(channel, "created_at", None)) if in_ticket else None
        if opened:
            lines.append(f"ticket opened: {opened}")
        return lines

    async def _send_paused_notice(self, channel: discord.abc.Messageable) -> None:
        """Once per channel per UTC day, so a paused bot doesn't spam every message."""
        today = datetime.now(timezone.utc).date()
        if self._paused_notices.get(channel.id) == today:
            return
        self._paused_notices[channel.id] = today
        await self._send_messages_with_typing(channel, [AI_PAUSED_TEXT])

    async def _ping_escalation_roles(self, channel: discord.TextChannel, requester_id: int) -> None:
        role_ids = self.bot.settings.ai_ticket_escalation_role_ids
        if not role_ids:
            return
        try:
            # Edited messages never ping, so the role mention goes out as a fresh message.
            await channel.send(
                " ".join(f"<@&{role_id}>" for role_id in role_ids)
                + f" <@{requester_id}> still needs help, the AI couldn't solve this one.",
                allowed_mentions=discord.AllowedMentions(roles=[discord.Object(id=role_id) for role_id in role_ids]),
            )
        except discord.HTTPException:
            log.exception("Failed to ping staff for escalated ticket", extra={"channel_id": channel.id})

    async def _process_support_message(self, message: discord.Message) -> None:
        channel = message.channel
        if not hasattr(channel, "send"):
            return

        settings = self.bot.settings
        in_ticket = isinstance(channel, discord.TextChannel) and _is_ticket_channel(channel, settings=settings)
        in_dm = isinstance(channel, discord.DMChannel)
        if in_ticket and _is_staff_ticket_message(message, in_ticket=True, settings=settings):
            return
        bot_pinged = _is_pinging_bot(message, self.bot.user)
        mention_request = bot_pinged and _has_support_request_content(message, self.bot.user)
        dm_request = in_dm and _has_support_request_content(message, self.bot.user)

        if not in_ticket and not mention_request and not dm_request:
            return
        member = (
            message.author
            if isinstance(message.author, discord.Member)
            else await self._resolve_member_for_user(message.author)
        )
        author_has_support_access = member is not None and can_use_ai_support(member, settings=settings)
        if (mention_request or dm_request) and not in_ticket and not author_has_support_access:
            return

        if in_ticket and channel.id in self._escalated_ticket_channels:
            return
        has_image = any(_is_image_attachment(attachment) for attachment in message.attachments)
        if in_ticket or _is_reply_to_bot(message, self.bot.user):
            # Follow-ups ("still broken", "1.20.1") must reach the model; only pure acks are skipped.
            if not has_image and _is_ack_message(_strip_bot_mentions(message.content, self.bot.user)):
                return
        elif _message_support_intent(message, self.bot.user) == SUPPORT_INTENT_UNCLEAR:
            return
        if ai_budget.is_paused(settings):
            await self._send_paused_notice(channel)
            return
        events = self._rate_limit_state.record(
            self._support_events, (message.author.id, 0), time.monotonic(), SUPPORT_RATE_LIMIT_WINDOW_SECONDS
        )
        if len(events) > SUPPORT_RATE_LIMIT_CALLS:
            log.warning(
                "AI support rate limit hit by %s",
                message.author.id,
                extra={"event": "ai_support_rate_limited", "channel_id": channel.id, "user_id": message.author.id},
            )
            if len(events) == SUPPORT_RATE_LIMIT_CALLS + 1 and (in_ticket or in_dm):
                await self._send_messages_with_typing(channel, [RATE_LIMITED_TEXT])
            return

        async with self._channel_locks[channel.id]:
            try:
                # Analyses are rendered inline on the triggering message, so the question text and
                # its screenshots stay one user turn (and are reused for later turns + the transcript).
                # Shielded: a newer message may cancel this task, but a request already sent still costs
                # tokens and must finish so ai_budget records it; only the stale reply is dropped.
                image_analyses = await asyncio.shield(self._extract_image_context(message))
                history = await self._build_history(
                    message,
                    in_ticket=in_ticket,
                    image_analyses=image_analyses,
                )
                context_lines = self._support_context_lines(
                    message, member=member, in_ticket=in_ticket, in_dm=in_dm
                )
                async with channel.typing():
                    result = await asyncio.shield(run_support_agent(
                        messages=history,
                        enabled_tools=SUPPORT_TOOL_NAMES,
                        language_hint=None,
                        user_id=message.author.id,
                        channel_id=channel.id,
                        ticket_conversation=in_ticket,
                        bot=self.bot,
                        settings=settings,
                        context_lines=context_lines,
                        requester_is_staff=member is not None and is_staff(member, settings=settings),
                        channel_kind="ticket" if in_ticket else "dm" if in_dm else "public",
                    ))
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
                        await self._mark_ticket_escalated(channel.id)
                elif mention_request:
                    await self._send_messages_with_typing(
                        channel,
                        [
                            f"{message.author.mention} I ran into an error while processing that. "
                            "Please try again or open a support ticket."
                        ],
                    )
                return

            if result.get("paused"):
                await self._send_paused_notice(channel)
                return
            if _has_user_visible_tool_result(result["tool_results"]):
                return

            outgoing_messages: list[str] = []
            should_mark_escalated = False
            kind = result.get("kind")

            reply_text = result["reply"].strip()
            public = not (in_ticket or in_dm)
            if public and reply_text and reply_text != "(no reply)" and not can_post_publicly(result):
                reply_text = PUBLIC_FALLBACK_TEXT
            if reply_text and reply_text != "(no reply)":
                outgoing_messages.append(reply_text)
                should_mark_escalated = in_ticket and kind == "handoff"
            else:
                if in_ticket:
                    outgoing_messages.append(
                        "I could not confidently answer this from the uploaded knowledge. A staff member should review it."
                    )
                    should_mark_escalated = True
                elif mention_request:
                    outgoing_messages.append(
                        "I couldn't find a confident knowledge-backed answer for that. Please open a ticket if it needs follow-up."
                    )

            sent = await self._send_messages_with_typing(channel, outgoing_messages)
            if sent and should_mark_escalated:
                await self._mark_ticket_escalated(channel.id)
                if kind == "handoff":
                    await self._ping_escalation_roles(channel, message.author.id)
            elif sent and in_ticket and kind in (None, "answer") and await self._is_ticket_owner(channel, message.author.id):
                await self._maybe_prompt_resolution(
                    channel,
                    requester_id=message.author.id,
                    confidence=result.get("confidence"),
                    force=result["suggested_close"],
                    language=result.get("language", "en"),
                )

    async def answer_new_ticket(
        self,
        channel: discord.TextChannel,
        member: discord.Member,
        *,
        category_label: str,
        form_text: str,
        language: str,
    ) -> None:
        """First AI reply for a ticket opened through the tickets cog, from the intake form instead of a message."""
        settings = self.bot.settings
        if not settings.ai_support_enabled or ai_budget.is_paused(settings):
            return
        self._ticket_owners[channel.id] = member.id
        name = safe_name(member.display_name)
        try:
            async with channel.typing():
                result = await run_support_agent(
                    messages=[
                        ConversationMessage(
                            role="user",
                            content=form_text,
                            speaker_name=name,
                            speaker_id=str(member.id),
                            speaker_kind="requester",
                        )
                    ],
                    enabled_tools=SUPPORT_TOOL_NAMES,
                    language_hint=language,
                    user_id=member.id,
                    channel_id=channel.id,
                    ticket_conversation=True,
                    bot=self.bot,
                    settings=settings,
                    context_lines=[
                        "channel: support ticket",
                        f"requester: {name} (roles: member)",
                        f"ticket category: {category_label}",
                        "ticket opened: just now (the requester's intake form is the message below)",
                    ],
                    requester_is_staff=False,
                    channel_kind="ticket",
                )
        except Exception:
            log.exception("AI first reply failed for new ticket", extra={"channel_id": channel.id})
            return

        reply = result["reply"].strip()
        if result.get("paused") or _has_user_visible_tool_result(result["tool_results"]) or reply in ("", "(no reply)"):
            return
        if await self._send_messages_with_typing(channel, [reply]) and result.get("kind") == "handoff":
            await self._mark_ticket_escalated(channel.id)
            await self._ping_escalation_roles(channel, member.id)

    def _ticket_creator_ids(self) -> set[int]:
        """Bots whose first message mentions the ticket owner: Ticket Tool, and our own tickets cog."""
        return {TICKET_TOOL_BOT_ID, getattr(self.bot.user, "id", TICKET_TOOL_BOT_ID)}

    async def _is_ticket_owner(self, channel: discord.TextChannel, user_id: int) -> bool:
        """Ticket Tool's welcome message mentions the owner; only they may get the "solved?" buttons."""
        if channel.id not in self._ticket_owners:
            owner_id = None
            async for entry in channel.history(limit=5, oldest_first=True):
                if entry.author.id in self._ticket_creator_ids():
                    owner_id = next((mentioned.id for mentioned in entry.mentions if not mentioned.bot), None)
                    if owner_id is not None:
                        break
            self._ticket_owners[channel.id] = owner_id
        owner_id = self._ticket_owners[channel.id]
        return owner_id is None or owner_id == user_id

    async def _maybe_prompt_resolution(
        self,
        channel: discord.TextChannel,
        *,
        requester_id: int,
        confidence: float | None,
        force: bool,
        language: str,
    ) -> None:
        settings = self.bot.settings
        if confidence is not None:
            self._last_confidence[channel.id] = confidence
        probability = 1.0 if force else _resolve_prompt_probability(
            confidence,
            min_confidence=settings.ai_ticket_resolve_min_confidence,
            exponent=settings.ai_ticket_resolve_prompt_exponent,
        )
        log.info(
            "Resolve prompt roll",
            extra={
                "event": "ticket_resolve_prompt_roll",
                "channel_id": channel.id,
                "confidence": confidence,
                "probability": probability,
            },
        )
        if random.random() >= probability:
            return

        previous = self._resolve_prompts.pop(channel.id, None)
        if previous is not None:
            try:
                await previous.delete()
            except discord.HTTPException:
                pass

        embed = discord.Embed(
            description=f"**{_resolve_text(language)['question']}**",
            color=discord.Color.blurple(),
        )
        try:
            self._resolve_prompts[channel.id] = await channel.send(
                embed=embed,
                view=_build_resolve_view(requester_id, language),
            )
        except discord.HTTPException:
            log.exception("Failed to send ticket resolve prompt", extra={"channel_id": channel.id})

    async def _handle_resolve_answer(
        self,
        interaction: discord.Interaction,
        *,
        solved: bool,
        requester_id: int,
        language: str,
    ) -> None:
        settings = self.bot.settings
        channel = interaction.channel
        user = interaction.user
        if user.id != requester_id and not (
            isinstance(user, discord.Member) and is_staff(user, settings=settings)
        ):
            return await interaction.response.send_message(
                "Only the ticket owner can answer this.", ephemeral=True
            )
        if not isinstance(channel, discord.TextChannel) or not _is_ticket_channel(channel, settings=settings):
            return await interaction.response.send_message("This ticket is no longer open.", ephemeral=True)
        if channel.id in self._closing_channels:
            return await interaction.response.send_message("This ticket is already closing.", ephemeral=True)

        text = _resolve_text(language)
        self._resolve_prompts.pop(channel.id, None)
        embed = discord.Embed(
            description=text["thanks"] if solved else text["escalated"],
            color=discord.Color.green() if solved else discord.Color.orange(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        if solved:
            await self._close_ticket(
                channel,
                closed_by_id=user.id,
                requester_id=requester_id,
                resolved=True,
                delete_channel=True,
            )
            return

        await self._mark_ticket_escalated(channel.id)
        await self._ping_escalation_roles(channel, requester_id)

    async def _collect_transcript(
        self,
        channel: discord.TextChannel,
        requester_id: int | None,
    ) -> tuple[list[TranscriptLine], int | None]:
        messages = [
            entry
            async for entry in channel.history(limit=TRANSCRIPT_MESSAGE_LIMIT, oldest_first=True)
        ]
        if requester_id is None:
            # Ticket Tool's welcome message mentions the owner; fall back to the first non-staff human.
            requester_id = next(
                (
                    mentioned.id
                    for entry in messages
                    if entry.author.id in self._ticket_creator_ids()
                    for mentioned in entry.mentions
                    if not mentioned.bot
                ),
                None,
            ) or next(
                (
                    entry.author.id
                    for entry in messages
                    if not entry.author.bot and self._speaker_kind(entry, None) == "participant"
                ),
                None,
            )
        analyses = await self._cached_image_analyses(messages)
        lines: list[TranscriptLine] = []
        for entry in messages:
            if entry.author.bot and entry.author != self.bot.user:
                continue
            content = _message_content(entry, analyses)
            if not content:
                continue
            lines.append(
                TranscriptLine(
                    created_at=entry.created_at,
                    speaker_kind=self._speaker_kind(entry, requester_id),
                    speaker_name=getattr(entry.author, "display_name", entry.author.name),
                    content=content,
                )
            )
        return lines, requester_id

    def forget_archived(self, channel_id: int) -> None:
        """A re-opened ticket must be archivable again when it closes."""
        self._archived_channels.discard(channel_id)

    async def _build_page(self, channel: discord.TextChannel) -> StoredPage | None:
        """The in-house ticket cog owns HTML export; without it (or when it fails) the ticket just has no web page."""
        tickets = self.bot.get_cog("TicketsCog")
        if tickets is None:
            return None
        try:
            return await tickets.build_page(channel)
        except Exception:
            log.exception("Failed to build hosted ticket transcript", extra={"channel_id": channel.id})
            return None

    async def _close_ticket(
        self,
        channel: discord.TextChannel,
        *,
        closed_by_id: int | None,
        requester_id: int | None,
        resolved: bool | None,
        delete_channel: bool,
        announce: bool = True,
    ) -> bool:
        """Transcript → hosted HTML page → summarize → learn (vector store) → embed → record → optionally delete.
        Transcript is read first because Ticket Tool may delete the channel seconds later.
        resolved=None lets the summary decide (Ticket Tool closes don't say).
        Returns whether the transcript was saved for the panel."""
        archived = False
        if channel.id in self._closing_channels or channel.id in self._archived_channels:
            return archived
        self._closing_channels.add(channel.id)
        self._cancel_pending_task((channel.id, 0))
        self._escalated_ticket_channels.add(channel.id)
        settings = self.bot.settings
        try:
            closed_at = datetime.now(timezone.utc)
            lines, requester_id = await self._collect_transcript(channel, requester_id)
            transcript = render_transcript_text(lines, channel_name=channel.name)
            page = await self._build_page(channel)

            summary: TicketSummary | None = None
            try:
                if not ai_budget.is_paused(settings):
                    summary = await summarize_ticket(
                        lines,
                        model=settings.openai_ticket_summary_model,
                        timeout_seconds=settings.ai_support_timeout_seconds,
                    )
            except Exception:
                log.exception("Failed to summarize ticket", extra={"channel_id": channel.id})
            if summary is not None:
                # Written by a model from member text; it lands in staff channels and AI knowledge.
                summary = replace(
                    summary,
                    title=strip_links(defuse_mentions(summary.title)),
                    problem=strip_links(defuse_mentions(summary.problem)),
                    resolution=strip_links(defuse_mentions(summary.resolution)),
                )
            if resolved is None:
                resolved = bool(summary and summary.resolved)

            openai_file_id: str | None = None
            vector_store_id = _ticket_vector_store_id(settings)
            # Only tickets a staff member took part in may teach the AI, and only their lines are kept.
            staff_reviewed = any(line.speaker_kind == "staff" for line in lines)
            if summary is not None and summary.knowledge_worthy and staff_reviewed and vector_store_id:
                try:
                    openai_file_id = await upload_ticket_knowledge(
                        render_knowledge_markdown(summary, lines, closed_at=closed_at),
                        filename=ticket_knowledge_filename(channel.id, closed_at),
                        vector_store_id=vector_store_id,
                        attributes={
                            "source": "ticket",
                            "resolved": resolved,
                            "closed_at": closed_at.date().isoformat(),
                        },
                    )
                except Exception:
                    log.exception("Failed to upload ticket knowledge", extra={"channel_id": channel.id})

            confidence = self._last_confidence.pop(channel.id, None)
            embed = _build_close_embed(
                channel_name=channel.name,
                requester_id=requester_id,
                closed_by_id=closed_by_id,
                resolved=resolved,
                summary=summary,
                confidence=confidence,
                message_count=len(lines),
                open_for=closed_at - channel.created_at,
                added_to_knowledge=openai_file_id is not None,
                closed_at=closed_at,
                page_link=page_url(settings, page.token) if page else None,
                page_expires_at=page.expires_at if page else None,
            )
            try:
                await record_ticket_transcript(
                    channel_id=channel.id,
                    guild_id=channel.guild.id,
                    channel_name=channel.name,
                    requester_id=requester_id,
                    closed_by_id=closed_by_id,
                    resolved=resolved,
                    ai_confidence=confidence,
                    message_count=len(lines),
                    summary=summary,
                    transcript=transcript,
                    openai_file_id=openai_file_id,
                    html_token=page.token if page else None,
                    html_expires_at=page.expires_at if page else None,
                )
                await delete_image_analyses(channel.id)
                archived = True  # the panel's transcript row is the archive now
            except Exception:
                log.exception("Failed to record ticket transcript", extra={"channel_id": channel.id})

            self._archived_channels.add(channel.id)
            self._resolve_prompts.pop(channel.id, None)
            delay = max(int(settings.ai_ticket_close_delay_seconds or 0), 0)
            embed.description = (
                f"🔒 This ticket is closed and will be deleted in {delay}s."
                if delete_channel
                else "📋 Ticket summary and transcript saved."
            )
            try:
                if announce:
                    await channel.send(embed=embed)
                elif page is not None:
                    await channel.send(f"🧾 A transcript of this ticket was created: {page_url(settings, page.token)}")
            except discord.HTTPException:
                # Expected when Ticket Tool already deleted the channel.
                log.info("Could not post close embed in ticket", extra={"channel_id": channel.id})
            if delete_channel:
                await asyncio.sleep(delay)
                await channel.delete(reason="Requester confirmed the AI solved the ticket")
        except discord.HTTPException:
            log.exception("Failed to close ticket channel", extra={"channel_id": channel.id})
            try:
                await channel.send("I couldn't delete this channel (missing Manage Channels?). A staff member can delete it manually.")
            except discord.HTTPException:
                pass
        finally:
            self._closing_channels.discard(channel.id)
        return archived

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
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        parsed = _parse_resolve_custom_id((interaction.data or {}).get("custom_id", ""))
        if parsed is None:
            return
        solved, requester_id, language = parsed
        await self._handle_resolve_answer(
            interaction,
            solved=solved,
            requester_id=requester_id,
            language=language,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        settings = self.bot.settings
        closer_id = _ticket_tool_closer_id(message)
        if closer_id is not False and _is_ticket_channel(message.channel, settings=settings):
            await self._close_ticket(
                message.channel,
                closed_by_id=closer_id,
                requester_id=None,
                resolved=None,
                delete_channel=False,
            )
            return

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
