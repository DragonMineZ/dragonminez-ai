import asyncio
import contextlib
import io
import logging
import random
import re
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands

from bulmaai.cogs import dmzdebug as dmzdebug_cog
from bulmaai.cogs import log_parser as log_parser_cog
from bulmaai.services import ai_asks as ask_store
from bulmaai.services import ai_budget
from bulmaai.services import ai_feedback as feedback_store
from bulmaai.services.ai_oneshot import response_text
from bulmaai.services.ai_guard import (
    BLOCKING_FLAGS,
    can_post_publicly,
    defuse_mentions,
    safe_name,
    screen_reply,
    strip_links,
)
from bulmaai.services.ai_tools import SUPPORT_TOOL_NAMES
from bulmaai.services.moderation import ModerationState
from bulmaai.services.ai_client import (
    ConversationMessage,
    client as vision_client,
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
from bulmaai.services.ticket_intake import owner_says_solved
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
    save_ticket_knowledge,
    stored_knowledge_filename,
    summarize_ticket,
    ticket_knowledge_filename,
)
from bulmaai.services.tickets import (
    STATUS_OPEN,
    ClosedGap,
    get_open_tickets_by_owner,
    get_ticket_by_channel,
    hide_closed_messages,
    note_ticket_message,
)
from bulmaai.ui import ai_asks, ai_feedback
from bulmaai.ui.ticket_views import CATEGORIES, TicketModal, msg_already_open, panel_view
from bulmaai.ui.v2 import while_busy
from bulmaai.utils.dmzdebug_parser import looks_like_dmzdebug, parse_debug
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.forwards import forwarded_content, full_attachments, full_content
from bulmaai.utils.log_parser import parse_log
from bulmaai.utils.permissions import (
    can_use_ai_support,
    has_any_allowed_role,
    has_patreon_access_role,
    is_allowed_guild_id,
    is_staff,
)

log = logging.getLogger(__name__)

LOG_ATTACHMENT_EXTENSIONS = (".log", ".txt")
IMAGE_ATTACHMENT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
DM_UPLOAD_LIMIT = 25 * 1024 * 1024  # when a DM interaction doesn't say what the bot may upload there
BUG_REPORTS_COG = "BugReportsCog"
BUG_REPORT_MAX_FILES = 3  # the requester's logs and screenshots a report_bug post carries
BUG_POST_FALLBACK_NAME = "Bug report"
DISCORD_MESSAGE_LIMIT = 1900
# V2 cards allow 4000 characters of text per message; leave room for the footer.
CARD_TEXT_LIMIT = 3500
AI_CARD_COLOR = discord.Color.from_rgb(88, 101, 242)
AI_FOOTER = "-# Bulma is AI and can make mistakes. · Bulma es una IA y puede equivocarse."
MAX_IMAGE_ANALYSIS_CHARS = 1000
# ponytail: per-process cache of parsed log summaries by attachment id; a restart only means re-parsing.
_LOG_SUMMARIES: dict[int, str | None] = {}
_LOG_SUMMARY_CACHE_SIZE = 256
# ponytail: a /ask question isn't a channel message, so history can't see it. Remember it by answer message id
# (in memory, a restart forgets it; the context window is only 30 min anyway).
_ASK_QUESTIONS: dict[int, tuple[int, str, str]] = {}
_ASK_QUESTIONS_SIZE = 256


def remember_ask_question(answer_id: int, user_id: int, name: str, question: str) -> None:
    _ASK_QUESTIONS[answer_id] = (user_id, name, question)
    if len(_ASK_QUESTIONS) > _ASK_QUESTIONS_SIZE:
        _ASK_QUESTIONS.pop(next(iter(_ASK_QUESTIONS)))
# ponytail: hard cap on transcript size; page through history if tickets ever exceed it.
TRANSCRIPT_MESSAGE_LIMIT = 1000
RESOLVE_BUTTON_PREFIX = "ticket_resolved:"
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
# ponytail: EN|ES phrase list; misses odd phrasings, the model's [kind: handoff] still covers those.
HUMAN_REQUEST_RE = re.compile(
    r"(?i)\b((actual|real|human)\s+(person|human|staff|mod|helper|people)|talk\s+to\s+(a\s+)?(human|staff|mod|someone)|"
    r"need\s+(a\s+)?(human|staff)|persona\s+real|un\s+humano|hablar\s+con\s+(un\s+)?(humano|staff|moderador|alguien))\b"
)
ACK_MESSAGE_RE = re.compile(
    r"(?i)^\W*((thanks?|thank (you|u)|thx|ty)( (so|very) much| a lot| bro| man)?|tysm|tyvm|ok|okay|k|kk|cool|nice|"
    r"great|perfect|got it|(muchas )?gracias|vale|listo|perfecto|dale|obrigad[oa]|valeu|beleza)\W*$"
)
# Messages of context (owner's last one included) the "is it solved?" check sees.
SOLVED_CHECK_CONTEXT_MESSAGES = 6
SOLVED_CLOSE_TEXT = {
    "en": "Glad it's sorted! 🎉 Thanks for reaching out, this ticket will be closed now.",
    "es": "¡Me alegra que esté resuelto! 🎉 Gracias por escribirnos, este ticket se cerrará ahora.",
    "pt": "Que bom que foi resolvido! 🎉 Obrigado pelo contato, este ticket será fechado agora.",
}
# A ticket's support language, as the model is told to answer in it.
LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "pt": "Portuguese"}
# ponytail: public-channel context window; widen if pings keep missing earlier chatter.
GENERAL_CONTEXT_MESSAGE_LIMIT = 15
# ponytail: a thread is one conversation, so no age cutoff there; its newest N messages plus what led into it.
THREAD_CONTEXT_MESSAGE_LIMIT = 30
THREAD_NAME_LIMIT = 90
HELP_THREAD_ARCHIVE_MINUTES = 1440
_THREAD_NAME_JUNK_RE = re.compile(
    r"<a?:\w+:\d+>|<[@#][!&]?\d+>|<t:\d+(?::\w)?>|(?i:\b[a-z][a-z0-9+.-]*://\S+)|[*_~`|>#\[\]()\\]+"
)
# ponytail: who each of Bulma's help threads is for, read off its starter once; a restart reads it again.
_THREAD_REQUESTERS_SIZE = 512
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
RESOLVE_FAILED_TEXT = "Something went wrong, please try again."
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


def _with_footer(text: str) -> str:
    """The answer, then a blank line, then the small-text block: wiki links (if any) and the AI footer."""
    body, _, last = text.rstrip().rpartition("\n")
    if body and last.startswith("-# 📖 "):
        return f"{body}\n\n{last}\n{AI_FOOTER}"
    return f"{text.rstrip()}\n\n{AI_FOOTER}"


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


def _channel_kind(channel: Any, *, settings) -> str:
    """Where an answer goes out, as ai_client's channel kinds: ticket, dm or public. Threads are never tickets."""
    if isinstance(channel, discord.DMChannel):
        return "dm"
    if _is_ticket_channel(channel, settings=settings):
        return "ticket"
    return "public"


def _is_support_thread(channel: Any, *, settings) -> bool:
    """A public thread of our servers (forum posts count) that Bulma answers in like a public channel. Private threads
    the bug-report forum's posts (cogs/bug_reports owns those) and threads inside tickets are left alone."""
    ticket_categories = {getattr(settings, "ai_ticket_category_id", None), getattr(settings, "ticket_closed_category_id", None)}
    return (
        isinstance(channel, discord.Thread)
        and channel.type == discord.ChannelType.public_thread
        and is_allowed_guild_id(getattr(getattr(channel, "guild", None), "id", None), settings)
        and channel.parent_id != getattr(settings, "bug_report_forum_channel_id", None)
        and getattr(getattr(channel, "parent", None), "category_id", None) not in ticket_categories - {None}
    )


async def _thread_starter(thread: discord.Thread) -> discord.Message | None:
    """The message a thread was started from (it has the thread's id): a forum post's first message, in the thread,
    or the text-channel message the thread was made off. None when it was deleted; other fetch errors raise."""
    if (cached := getattr(thread, "starting_message", None)) is not None:
        return cached
    parent = thread.parent
    source = parent if isinstance(parent, discord.TextChannel) else thread
    try:
        return await source.fetch_message(thread.id)
    except discord.NotFound:
        return None


def _thread_name(text: str, settings: Any) -> str:
    """A help thread's name from member text: one line without mentions, emoji codes, links or markdown, at most
    THREAD_NAME_LIMIT characters. "" when nothing is left or the text trips the reply guard (a banned word)."""
    name = " ".join(_THREAD_NAME_JUNK_RE.sub(" ", text or "").split())
    if len(name) > THREAD_NAME_LIMIT:
        name = name[: THREAD_NAME_LIMIT - 1].rstrip() + "…"
    screened, flags = screen_reply(name, settings)
    return "" if flags & BLOCKING_FLAGS else screened


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
    text = _strip_bot_mentions(full_content(message), bot_user)
    if len(text.strip()) >= 3:
        return True
    return any(_is_image_attachment(a) or _is_log_attachment(a) for a in full_attachments(message))


def _message_support_intent(
    message: discord.Message,
    bot_user: discord.ClientUser | None,
) -> SupportIntent:
    text = _strip_bot_mentions(full_content(message), bot_user)
    return classify_support_intent(
        text,
        has_image=any(_is_image_attachment(attachment) for attachment in full_attachments(message)),
    )


def _asks_for_human(text: str) -> bool:
    """The requester wants a person: hand off in code instead of trusting the model's marker."""
    return bool(HUMAN_REQUEST_RE.search(text))


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


def _is_log_attachment(attachment: discord.Attachment) -> bool:
    return attachment.filename.lower().endswith(LOG_ATTACHMENT_EXTENSIONS)


def _contains_log_attachment(message: discord.Message) -> bool:
    return any(_is_log_attachment(attachment) for attachment in full_attachments(message))


def _summarize_upload(text: str) -> str | None:
    """The same analysis the log/dmzdebug cards show, as text for the model. None if it's neither."""
    if looks_like_dmzdebug(text):
        return dmzdebug_cog.summary_for_ai(parse_debug(text))
    if log_parser_cog.looks_like_mc_log(text):
        return log_parser_cog.summary_for_ai(parse_log(text))
    return None


async def _log_analyses(messages: list[discord.Message]) -> dict[int, str]:
    """Parsed summaries of the log/dmzdebug files in these messages; the raw file never reaches the model."""
    out: dict[int, str] = {}
    for entry in messages:
        for attachment in full_attachments(entry):
            if not _is_log_attachment(attachment) or attachment.size > log_parser_cog.MAX_FILE_SIZE:
                continue
            if attachment.id not in _LOG_SUMMARIES:
                try:
                    text = (await attachment.read()).decode("utf-8", errors="replace")
                except Exception:
                    log.exception("Failed to read log attachment for AI context")
                    continue
                _LOG_SUMMARIES[attachment.id] = await asyncio.to_thread(_summarize_upload, text)
                if len(_LOG_SUMMARIES) > _LOG_SUMMARY_CACHE_SIZE:
                    _LOG_SUMMARIES.pop(next(iter(_LOG_SUMMARIES)))
            if summary := _LOG_SUMMARIES.get(attachment.id):
                out[attachment.id] = summary
    return out


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_ATTACHMENT_EXTENSIONS)


def _upload_limit(interaction: discord.Interaction) -> int:
    """How many bytes the bot may post in one message where this interaction happened: the guild's upload limit; in
    DMs what Discord says for this interaction, else DM_UPLOAD_LIMIT."""
    if (guild := getattr(interaction, "guild", None)) is not None:
        return guild.filesize_limit
    limit = getattr(interaction, "attachment_size_limit", None)
    return limit if isinstance(limit, int) and limit > 0 else DM_UPLOAD_LIMIT


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


def _component_dicts(message: discord.Message) -> list[dict]:
    """Every component of a message, nested ones included, as Discord payload dicts."""
    out: list[dict] = []

    def walk(items) -> None:
        for item in items or ():
            out.append(item)
            walk(item.get("components"))

    walk([component.to_dict() for component in getattr(message, "components", None) or ()])
    return out


def _card_texts(message: discord.Message) -> list[str]:
    return [item["content"] for item in _component_dicts(message) if item.get("type") == 10 and item.get("content")]


def _card_text(message: discord.Message) -> str:
    """The text of a V2 card (its `content` is empty), minus our "AI can make mistakes" footer, a recorded answer
    (that is the requester's turn, see _card_answer) and the thanks for a rating (ui/ai_feedback)."""
    lines = [
        text.replace(AI_FOOTER, "")
        for text in _card_texts(message)
        if not text.startswith(ai_asks.ANSWER_LINE_PREFIX) and text not in ai_feedback.THANKS_LINES
    ]
    return "\n".join(line.strip() for line in lines if line.strip())


def _card_answer(message: discord.Message) -> tuple[int, str] | None:
    """(user id, answer) given with the controls on this card, if any: a pick's label, a form's lines, a steps
    outcome, or the steps done so far on a steps card still in progress (read off its buttons)."""
    return ai_asks.read_answer(_card_texts(message)) or ai_asks.read_progress(
        [item.get("custom_id") for item in _component_dicts(message)]
    )


def _is_answer_card(message: Any, bot_user: discord.ClientUser | None) -> bool:
    """One of Bulma's support answers (a typed one's, Ask Bulma's, /ask's): her card with the AI footer, which only an
    answer's last card carries."""
    author_id = getattr(getattr(message, "author", None), "id", None)
    return (
        bot_user is not None
        and author_id == bot_user.id
        and any(AI_FOOTER in text for text in _card_texts(message))
    )


def _rebuilt(message: discord.Message, *items: discord.ui.Item) -> discord.ui.DesignerView:
    """The card's own text (not the text that belongs to its controls), then items."""
    own = [
        item["content"]
        for item in _component_dicts(message)
        if item.get("type") == 10 and item.get("content") and item.get("id") != ai_asks.CONTROLS_TEXT_ID
    ]
    return discord.ui.DesignerView(
        discord.ui.Container(*(discord.ui.TextDisplay(text) for text in own), *items, color=AI_CARD_COLOR),
        timeout=None,
    )


def _answered_card(
    message: discord.Message, lines: list[str], *, controls_text: str | None = None, notes: Sequence[str] = ()
) -> discord.ui.DesignerView:
    """The card as it was, without its controls, with the answer lines under it. controls_text replaces the text that
    belonged to the controls (a steps card's list, in its final state); notes go last (what an action did)."""
    controls = [discord.ui.TextDisplay(controls_text, id=ai_asks.CONTROLS_TEXT_ID)] if controls_text else []
    return _rebuilt(message, *controls, *(discord.ui.TextDisplay(text) for text in [*lines, *notes]))


def _steps_card(message: discord.Message, row: ask_store.AskRow, progress: int) -> discord.ui.DesignerView:
    """A steps card at step progress: its own text, then the list and that step's buttons."""
    return _rebuilt(message, *ai_asks.steps_items(row.spec, row.requester_id, row.language, progress))


# ponytail: only what the answer path reads off a discord.Message (and utils/forwards); add attributes as it grows.
class _ChoiceMessage:
    """Stands in for the message an answer on a card (a pick, a submitted form or an upload) replaces. Its id is the
    card's, so the general history, which always keeps the triggering message, keeps the card and the answer on it.
    attachments: an upload's files as reposted, so their screenshots get analyzed like a typed upload's. card: the
    answer card itself, which a follow-up in a text channel moves into a thread off (_open_help_thread)."""

    attachments = snapshots = mentions = ()
    reference = None

    def __init__(
        self,
        interaction: discord.Interaction,
        content: str,
        message_id: int,
        attachments: Sequence[Any] = (),
        card: discord.Message | None = None,
    ) -> None:
        self.author = interaction.user
        self.channel = interaction.channel
        self.guild = interaction.guild
        self.id = message_id
        self.content = self.clean_content = content
        self.created_at = datetime.now(timezone.utc)
        self.attachments = tuple(attachments)
        self.card = card


class _AskedMessage(_ChoiceMessage):
    """Stands in for the requester asking Bulma about a message (the target) with the "Ask Bulma" command: the target
    quoted as their turn, with its files (forwarded ones too), so screenshots and logs get analyzed as if they posted
    them. Its id is the target's: _build_history skips the target's own place in the channel and puts this turn last."""

    def __init__(self, interaction: discord.Interaction, target: discord.Message) -> None:
        author = target.author
        content = ai_asks.asked_content(getattr(author, "display_name", author.name), _message_text(target))
        super().__init__(interaction, content, target.id, full_attachments(target))
        self.target_author_id = author.id
        self.reply_to = target  # the answer replies to it, so the channel sees what it is about


def _message_text(message: discord.Message) -> str:
    """A message's text: its own, a card's, and what it forwards (no attachments)."""
    return "\n".join(p for p in (message.clean_content.strip(), _card_text(message), forwarded_content(message)) if p)


def _message_content(
    message: discord.Message,
    image_analyses: dict[int, str] | None = None,
) -> str:
    """Text plus attachment markers. Images only show as text when analyzed before; never re-processed."""
    content = _message_text(message)
    attachment_lines: list[str] = []
    for attachment in full_attachments(message):
        analysis = (image_analyses or {}).get(getattr(attachment, "id", None))
        if analysis and _is_image_attachment(attachment):
            attachment_lines.append(f"[Image: {analysis[:MAX_IMAGE_ANALYSIS_CHARS]}]")
        elif analysis and _is_log_attachment(attachment):
            attachment_lines.append(f"[Log analysis of {attachment.filename}]\n{analysis}")
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


def _card(text: str, color: discord.Color = AI_CARD_COLOR, *rows: discord.ui.ActionRow) -> discord.ui.DesignerView:
    return discord.ui.DesignerView(discord.ui.Container(discord.ui.TextDisplay(text), *rows, color=color), timeout=None)


def _member_roles(member: discord.Member | None, settings: Any) -> str:
    """What the model may assume about someone: support flags plus their actual Discord role names."""
    if member is None:
        return "unknown"
    flags = []
    if is_staff(member, settings=settings):
        flags.append("staff")
    if has_patreon_access_role(member, settings=settings):
        flags.append("patron")
    if has_any_allowed_role(member, settings.dev_jar_tester_role_ids):
        flags.append("tester")
    names = [safe_name(role.name) for role in reversed(getattr(member, "roles", [])[1:])][:15]
    return f"{', '.join(flags) or 'member'}; Discord roles: {', '.join(names) or 'none'}"


def _language_line(language: str) -> str:
    name = LANGUAGE_NAMES.get(language, "English")
    return f"support language: {name}. Always reply in {name}, even if the requester writes in another language."


def _resolve_text(language: str) -> dict[str, str]:
    return RESOLVE_PROMPT_TEXT.get(language, RESOLVE_PROMPT_TEXT["en"])


def _build_resolve_view(requester_id: int, language: str) -> discord.ui.DesignerView:
    """Buttons are handled by the cog's on_interaction via custom_id, so they survive restarts."""
    text = _resolve_text(language)
    row = discord.ui.ActionRow(
        discord.ui.Button(
            label=text["yes"],
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"{RESOLVE_BUTTON_PREFIX}yes:{requester_id}:{language}",
        ),
        discord.ui.Button(
            label=text["no"],
            style=discord.ButtonStyle.secondary,
            emoji="🙋",
            custom_id=f"{RESOLVE_BUTTON_PREFIX}no:{requester_id}:{language}",
        ),
    )
    return _card(f"<@{requester_id}> **{text['question']}**", AI_CARD_COLOR, row)


def _parse_resolve_custom_id(custom_id: str) -> tuple[bool, int, str] | None:
    if not custom_id.startswith(RESOLVE_BUTTON_PREFIX):
        return None
    try:
        answer, requester_id, language = custom_id.removeprefix(RESOLVE_BUTTON_PREFIX).split(":")
        return answer == "yes", int(requester_id), language
    except ValueError:
        return None


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
        self._working: set[int] = set()  # "solved?" prompts whose answer is still being handled (ui.v2.while_busy)
        # ponytail: in-memory dedupe; reopening a ticket clears its entry (forget_archived), a restart clears all.
        self._archived_channels: set[int] = set()
        self._paused_notices: dict[int, date] = {}
        self._rate_limit_state = ModerationState()
        self._support_events: dict[tuple[int, int], list[float]] = defaultdict(list)
        # ponytail: one entry per ticket channel answered; tiny, cleared on restart.
        self._ticket_owners: dict[int, int | None] = {}
        # Ticket channel → its support language (None: no tickets row); TicketsCog drops an entry when it changes.
        self._ticket_languages: dict[int, str | None] = {}
        # Bulma's thread → the member it is for (None: not a help thread); see _help_thread_requester.
        self._thread_requesters: dict[int, int | None] = {}

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
        changed = channel_id not in self._escalated_ticket_channels
        self._escalated_ticket_channels.add(channel_id)
        self._cancel_pending_task((channel_id, 0))
        try:
            await set_ticket_ai_disabled(channel_id, True)
        except Exception:
            log.exception("Failed to persist AI ticket disabled state for channel %s", channel_id)
        if changed:
            await self._sync_ticket_button(channel_id, False)

    async def _sync_ticket_button(self, channel_id: int, enabled: bool) -> None:
        """Best effort: the ticket's "AI support: On/Off" button shows the new state."""
        tickets = self.bot.get_cog("TicketsCog")
        if tickets is None:
            return
        try:
            await tickets.refresh_ai_button(channel_id, enabled)
        except Exception:
            log.warning("Couldn't refresh the AI support button in %s", channel_id, exc_info=True)

    def is_ticket_ai_enabled(self, channel_id: int) -> bool:
        return channel_id not in self._escalated_ticket_channels

    async def set_ticket_ai_enabled(self, channel_id: int, enabled: bool) -> None:
        """Shared by /aisupport and the admin panel."""
        if not enabled:
            await self._mark_ticket_escalated(channel_id)
            return
        changed = channel_id in self._escalated_ticket_channels
        self._escalated_ticket_channels.discard(channel_id)
        try:
            await set_ticket_ai_disabled(channel_id, False)
        except Exception:
            log.exception("Failed to persist AI ticket enabled state for channel %s", channel_id)
        if changed:
            await self._sync_ticket_button(channel_id, True)

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

    @discord.message_command(
        name="Ask Bulma", contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm}
    )
    async def ask_bulma(self, ctx: discord.ApplicationContext, message: discord.Message) -> None:
        """Right-click a message → Apps → Ask Bulma: the invoker asks Bulma about it (_AskedMessage). Past the gates
        the click gets an ephemeral "On it…" at once, and the answer is posted in the channel like a typed one's."""
        interaction = ctx.interaction
        refusal, language, context = await self._ask_bulma_gate(interaction, message)
        if refusal:
            return await ctx.respond(ai_asks.text(language, refusal), ephemeral=True)
        await ctx.respond(ai_asks.text(language, "on_it"), ephemeral=True)
        self._answer_pick(_AskedMessage(interaction, message), **context)

    async def _ask_bulma_gate(
        self, interaction: discord.Interaction, target: discord.Message
    ) -> tuple[str | None, str, dict[str, Any]]:
        """(why Ask Bulma can't answer, as a ui/ai_asks.TEXT key, or None; the language to say it in: the ticket's, else
        English; the _answer_support arguments). The gates are a typed request's (_process_support_message): a ping's
        in public channels and threads, the ticket's in tickets (AI on, still open), DM support's in DMs. Our own cards
        may be asked about, other bots' messages not."""
        settings = self.bot.settings
        channel, user = interaction.channel, interaction.user
        kind = _channel_kind(channel, settings=settings)
        in_ticket, in_dm = kind == "ticket", kind == "dm"
        language = (await self._ticket_language(channel.id) or "en") if in_ticket else "en"
        context: dict[str, Any] = {
            "in_ticket": in_ticket, "in_dm": in_dm, "mention_request": kind == "public", "member": None
        }

        def refuse(key: str) -> tuple[str, str, dict[str, Any]]:
            return key, language, context

        # Like a ping: only text channels and public threads (not the bug forum's) of our servers, or a DM.
        answerable = isinstance(channel, discord.TextChannel) or _is_support_thread(channel, settings=settings)
        in_our_server = answerable and is_allowed_guild_id(getattr(interaction, "guild_id", None), settings)
        if not settings.ai_support_enabled or not (in_dm or in_our_server):
            return refuse("ask_off")
        if (target.author.bot or getattr(target, "webhook_id", None)) and target.author.id != getattr(
            self.bot.user, "id", None
        ):
            return refuse("ask_bot")
        if not _message_text(target) and not full_attachments(target):
            return refuse("ask_empty")
        if in_ticket and not self.is_ticket_ai_enabled(channel.id):
            return refuse("ask_off")
        if not (in_ticket or in_dm) and await self._is_closed_ticket(channel):
            return refuse("closed")
        member = user if isinstance(user, discord.Member) else await self._resolve_member_for_user(user)
        context["member"] = member
        if not in_ticket and not (member is not None and can_use_ai_support(member, settings=settings)):
            return refuse("ask_no_access")
        if ai_budget.is_paused(settings):
            return refuse("paused")
        events = self._rate_limit_state.record(
            self._support_events, (user.id, 0), time.monotonic(), SUPPORT_RATE_LIMIT_WINDOW_SECONDS
        )
        if len(events) > SUPPORT_RATE_LIMIT_CALLS:
            return refuse("rate_limited")
        return None, language, context

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

    async def ticket_closed(self, channel_id: int) -> None:
        """TicketsCog closed it: an answer still being written, and any "solved?" prompt, would land after the close."""
        self._cancel_pending_task((channel_id, 0))
        self.forget_ticket_language(channel_id)
        prompt = self._resolve_prompts.pop(channel_id, None)
        if prompt is not None:
            try:
                await prompt.delete()
            except discord.HTTPException:
                pass

    async def _ticket_language(self, channel_id: int) -> str | None:
        """The ticket's support language; None without a tickets row, or when the lookup fails (that isn't cached)."""
        if channel_id not in self._ticket_languages:
            try:
                ticket = await get_ticket_by_channel(channel_id)
            except Exception:
                log.exception("Could not look up ticket language", extra={"channel_id": channel_id})
                return None
            self._ticket_languages[channel_id] = ticket.language if ticket else None
        return self._ticket_languages[channel_id]

    def forget_ticket_language(self, channel_id: int) -> None:
        self._ticket_languages.pop(channel_id, None)

    async def _awaiting_owner(self, channel_id: int) -> None:
        """Our answer in a ticket starts the owner's reply countdown (TicketsCog._remind_owners)."""
        try:
            await note_ticket_message(channel_id, self.bot.user.id)
        except Exception:
            log.exception("Could not track ticket reply", extra={"channel_id": channel_id})

    async def _send_messages_with_typing(
        self,
        channel: discord.TextChannel,
        messages: list[str],
        *,
        ai_answer: bool = False,
        ask: dict[str, Any] | None = None,
        requester_id: int | None = None,
        language: str | None = None,
        feedback: bool = False,
        response_id: str | None = None,
        reply_to: discord.Message | None = None,
    ) -> bool:
        """Each chunk is a V2 card; ai_answer adds the "AI can make mistakes" footer to the last one, and ask (an
        ask_requester / offer_action spec, ui/ai_asks) puts its controls on it for requester_id only, saved in ai_asks.
        Chunks leave room for the text the controls bring (a steps list and its outcome line). feedback (without ask)
        puts the 👍/👎 row there instead, saved in ai_feedback with the reply's response_id."""
        limit = CARD_TEXT_LIMIT - ai_asks.reserved_text(ask)
        chunks = [
            chunk
            for message in messages
            if message
            for chunk in _chunk_discord_message(message, limit)
            if chunk.strip()
        ]
        if not chunks:
            return True

        if ai_answer:
            chunks[-1] = _with_footer(chunks[-1])
        views = [_card(chunk) for chunk in chunks]
        feedback = feedback and not ask and requester_id is not None
        if ask and requester_id is not None:
            views[-1] = _card(chunks[-1], AI_CARD_COLOR, *ai_asks.ask_rows(ask, requester_id, language))
        elif feedback:
            views[-1] = _card(chunks[-1], AI_CARD_COLOR, ai_feedback.feedback_row(requester_id))
        typing_lead_seconds = max(self.bot.settings.ai_support_typing_lead_seconds, 0)
        allowed_mentions = discord.AllowedMentions.none()  # none(): a reply doesn't ping its author either
        # Only the first card replies (reply_to); a deleted target just sends it unreplied.
        references = [reply_to.to_reference(fail_if_not_exists=False) if reply_to else None] + [None] * (len(views) - 1)
        try:
            if typing_lead_seconds <= 0:
                for view, reference in zip(views, references):
                    sent = await channel.send(view=view, allowed_mentions=allowed_mentions, reference=reference)
            else:
                async with channel.typing():
                    await asyncio.sleep(typing_lead_seconds)
                    for view, reference in zip(views, references):
                        sent = await channel.send(view=view, allowed_mentions=allowed_mentions, reference=reference)
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
        if ask and requester_id is not None:
            try:
                # Clicks read the spec from this row; without it they answer that the question expired.
                await ask_store.save(
                    message_id=sent.id,
                    channel_id=channel.id,
                    requester_id=requester_id,
                    spec=ask,
                    language=language or "en",
                )
            except Exception:
                log.exception("Could not save answer controls", extra={"channel_id": channel.id, "message_id": sent.id})
        elif feedback:
            try:
                # Without it a rating still counts (feedback_store.rate), only the response id is lost.
                await feedback_store.save(
                    message_id=sent.id,
                    channel_id=channel.id,
                    requester_id=requester_id,
                    response_id=response_id,
                    language=language or "en",
                )
            except Exception:
                log.exception("Could not save answer feedback", extra={"channel_id": channel.id, "message_id": sent.id})
        return True

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
        if (uploaded := self._upload_turn(message, requester_id, image_analyses)) is not None:
            return uploaded

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

    def _choice_turn(self, entry: discord.Message, requester_id: int | None) -> ConversationMessage | None:
        """The answer given on one of our cards (a pick, or a form's "label: value" lines), as the answerer's turn
        right after the card's question."""
        if entry.author != self.bot.user or (answer := _card_answer(entry)) is None:
            return None
        user_id, label = answer
        return self._member_turn(entry, user_id, requester_id, label, getattr(entry, "edited_at", None))

    def _upload_turn(
        self, entry: discord.Message, requester_id: int | None, analyses: dict[int, str] | None = None
    ) -> ConversationMessage | None:
        """Files a member uploaded with an Upload button, reposted on our card (_handle_ask_file): their turn, the
        files rendered as _message_content renders a typed upload's (log and screenshot analyses)."""
        if entry.author != self.bot.user or not full_attachments(entry):
            return None
        if (upload := ai_asks.read_upload(_card_texts(entry))) is None:
            return None
        content = _message_content(entry, analyses)
        return self._member_turn(entry, upload[0], requester_id, content, getattr(entry, "created_at", None))

    def _member_turn(
        self, entry: discord.Message, user_id: int, requester_id: int | None, content: str, at: datetime | None
    ) -> ConversationMessage:
        """A member's answer recorded on one of our cards, as their turn."""
        guild = getattr(entry, "guild", None)
        member = guild.get_member(user_id) if guild is not None else None
        turn = ConversationMessage(
            role="user",
            content=content,
            speaker_name=safe_name(member.display_name) if member is not None else "member",
            speaker_id=str(user_id),
            speaker_kind="requester" if user_id == requester_id else "participant",
        )
        if age := _relative_age(at):
            turn["age"] = age
        return turn

    async def _cached_image_analyses(self, messages: list[discord.Message]) -> dict[int, str]:
        attachment_ids = [
            attachment.id
            for entry in messages
            for attachment in full_attachments(entry)
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
        skip_id: int | None = None,
    ) -> list[ConversationMessage]:
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return []

        # Newest N, then oldest first: history(oldest_first=True) would return the ticket's FIRST N.
        messages = [
            entry
            async for entry in channel.history(limit=self.bot.settings.ai_support_history_limit)
            if entry.id != skip_id
        ]
        messages.reverse()
        requester_id = message.author.id
        analyses = {**await self._cached_image_analyses(messages), **await _log_analyses(messages), **image_analyses}

        history: list[ConversationMessage] = []
        for entry in messages:
            serialized = self._serialize_message(entry, requester_id=requester_id, image_analyses=analyses)
            if serialized is not None:
                history.append(serialized)
            if (picked := self._choice_turn(entry, requester_id)) is not None:
                history.append(picked)
        return history

    async def _build_general_history(
        self,
        message: discord.Message,
        image_analyses: dict[int, str],
        skip_id: int | None = None,
    ) -> list[ConversationMessage]:
        channel = message.channel
        if not hasattr(channel, "history"):
            return []

        author_id = message.author.id
        if isinstance(channel, discord.Thread):
            relevant_messages = await self._thread_context(channel, skip_id)
        else:
            cutoff = datetime.now(timezone.utc) - GENERAL_CONTEXT_MAX_AGE
            relevant_messages = [
                entry
                async for entry in channel.history(limit=GENERAL_CONTEXT_MESSAGE_LIMIT)
                if entry.id != skip_id and (entry.id == message.id or entry.created_at >= cutoff)
            ]
            relevant_messages.reverse()
        analyses = {
            **await self._cached_image_analyses(relevant_messages),
            **await _log_analyses(relevant_messages),
            **image_analyses,
        }

        history: list[ConversationMessage] = []
        for entry in relevant_messages:
            if asked := _ASK_QUESTIONS.get(entry.id):
                asker_id, asker_name, question = asked
                history.append(
                    ConversationMessage(
                        role="user",
                        content=question,
                        speaker_name=asker_name,
                        speaker_id=str(asker_id),
                        speaker_kind="requester" if asker_id == author_id else "participant",
                    )
                )
            serialized = self._serialize_message(entry, requester_id=author_id, image_analyses=analyses)
            if serialized is not None:
                history.append(serialized)
            if (picked := self._choice_turn(entry, author_id)) is not None:
                history.append(picked)
        return history

    async def _thread_context(self, thread: discord.Thread, skip_id: int | None = None) -> list[discord.Message]:
        """A thread's messages for the model, oldest first, with no age cutoff: what led into it (_thread_lead), then
        its newest THREAD_CONTEXT_MESSAGE_LIMIT messages. A forum post's starter is the thread's first message, so it
        is only fetched when the thread outgrew the window."""
        own = [entry async for entry in thread.history(limit=THREAD_CONTEXT_MESSAGE_LIMIT)]
        own.reverse()
        lead = [] if any(entry.id == thread.id for entry in own) else await self._thread_lead(thread)
        return [entry for entry in (*lead, *own) if entry.id != skip_id]

    async def _thread_lead(self, thread: discord.Thread) -> list[discord.Message]:
        """What a thread was started from, oldest first: a forum post's first message; the text-channel message it was
        made off; for a help thread Bulma opened, the channel's conversation up to that message too (public context
        window, counted back from it), so the question and her earlier answer stay in view after the move."""
        parent = thread.parent
        try:
            if not isinstance(parent, discord.TextChannel):
                starter = await _thread_starter(thread)
                return [starter] if starter is not None else []
            help_thread = self._is_help_thread(thread)
            # before the thread's id + 1: the message it was made off (same id) is the newest one returned.
            entries = [
                entry
                async for entry in parent.history(
                    limit=GENERAL_CONTEXT_MESSAGE_LIMIT if help_thread else 1, before=discord.Object(id=thread.id + 1)
                )
            ]
        except discord.HTTPException:
            log.info("Couldn't read what a thread was started from", exc_info=True, extra={"channel_id": thread.id})
            return []
        started = next((entry.created_at for entry in entries if entry.id == thread.id), None)
        cutoff = (started or discord.utils.snowflake_time(thread.id)) - GENERAL_CONTEXT_MAX_AGE
        return [
            entry
            for entry in reversed(entries)
            if entry.id == thread.id or (help_thread and entry.created_at >= cutoff)
        ]

    def _is_help_thread(self, channel: Any) -> bool:
        """A thread Bulma made off a message in a text channel: a help thread (_open_help_thread) or, say, a dev jar
        build's feedback thread; _help_thread_requester tells them apart."""
        return (
            isinstance(channel, discord.Thread)
            and channel.owner_id == getattr(self.bot.user, "id", None)
            and isinstance(channel.parent, discord.TextChannel)
        )

    def _remember_thread_requester(self, thread_id: int, requester_id: int | None) -> None:
        self._thread_requesters[thread_id] = requester_id
        if len(self._thread_requesters) > _THREAD_REQUESTERS_SIZE:
            self._thread_requesters.pop(next(iter(self._thread_requesters)))

    def _starter_requester(self, starter: discord.Message | None) -> int | None:
        """Who a help thread is for, read off the message it was made off: the member whose reply to Bulma's answer it
        was, or, for her answer card, whoever answered on it (the pick, form or steps line). None for anything else."""
        if starter is None:
            return None
        author = starter.author
        if author.id == getattr(self.bot.user, "id", None):
            answer = _card_answer(starter) if _is_answer_card(starter, self.bot.user) else None
            return answer[0] if answer is not None else None
        if author.bot or not _is_reply_to_bot(starter, self.bot.user):
            return None
        return author.id

    async def _help_thread_requester(self, channel: Any) -> int | None:
        """The member a help thread of Bulma's is for (they're answered there without a ping, as in a ticket); None
        anywhere else. It comes from Discord (the thread's starter), so it survives a restart; cached per thread."""
        if not self._is_help_thread(channel):
            return None
        if channel.id not in self._thread_requesters:
            try:
                starter = await _thread_starter(channel)
            except discord.HTTPException:
                log.info("Couldn't read a help thread's starter", exc_info=True, extra={"channel_id": channel.id})
                return None  # not cached: the next message tries again
            self._remember_thread_requester(channel.id, self._starter_requester(starter))
        return self._thread_requesters[channel.id]

    def _follow_up_source(self, message: discord.Message | _ChoiceMessage) -> tuple[Any, Any]:
        """(the message a follow-up's thread is made off, Bulma's earlier answer card) when message continues a
        conversation with her in a public text channel; (None, None) otherwise. A pick, form or steps outcome on her
        answer card: the card, which records it. A typed reply to her answer: the reply. Either way the thread starts
        at the end of the channel's conversation, so _thread_lead finds all of it before the starter."""
        channel = message.channel
        if (
            not isinstance(channel, discord.TextChannel)
            or _is_ticket_channel(channel, settings=self.bot.settings)
            or isinstance(message, _AskedMessage)
        ):
            return None, None
        if isinstance(message, _ChoiceMessage):
            return (message.card, message.card) if message.card is not None else (None, None)
        reference = getattr(message, "reference", None)
        card = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
        return (message, card) if _is_answer_card(card, self.bot.user) else (None, None)

    async def _answered_question(self, channel: discord.TextChannel, card: Any, requester_id: int) -> str | None:
        """The question an answer card answered: a /ask question, the message an Ask Bulma answer replies to, else the
        requester's latest message before the card within the public context window."""
        if asked := _ASK_QUESTIONS.get(card.id):
            return asked[2]
        target = getattr(getattr(card, "reference", None), "resolved", None)
        if target is not None and hasattr(target, "clean_content"):
            return _message_text(target)
        cutoff = card.created_at - GENERAL_CONTEXT_MAX_AGE
        try:
            async for entry in channel.history(limit=GENERAL_CONTEXT_MESSAGE_LIMIT, before=card):
                if entry.created_at < cutoff:
                    break
                if entry.author.id == requester_id:
                    return _message_text(entry)
        except discord.HTTPException:
            log.info("Couldn't read the question behind an answer", exc_info=True, extra={"channel_id": channel.id})
        return None

    async def _open_help_thread(self, message: discord.Message | _ChoiceMessage) -> discord.Thread | None:
        """A follow-up in a public text channel (a reply to Bulma's answer, an answer on its card) is answered in a new
        thread made off _follow_up_source's message and named after the question. None (answer in the channel, as
        before) when it isn't a follow-up or the thread can't be made (no permission, the message has one already)."""
        source, card = self._follow_up_source(message)
        if source is None:
            return None
        settings = self.bot.settings
        question = await self._answered_question(message.channel, card, message.author.id)
        typed = None if isinstance(message, _ChoiceMessage) else _message_text(message)
        name = ""
        for text in (question, typed):
            if text and (name := _thread_name(text, settings)):
                break
        name = name or safe_name(getattr(message.author, "display_name", message.author.name))
        try:
            thread = await source.create_thread(name=name, auto_archive_duration=HELP_THREAD_ARCHIVE_MINUTES)
        except (discord.HTTPException, discord.InvalidArgument):
            # INFO: a channel where Bulma may not make threads would warn staff on every follow-up.
            log.info("Couldn't open a help thread", exc_info=True, extra={"channel_id": message.channel.id})
            return None
        except Exception:
            log.exception("Opening a help thread failed", extra={"channel_id": message.channel.id})
            return None
        self._remember_thread_requester(thread.id, message.author.id)
        return thread

    async def _build_history(
        self,
        message: discord.Message,
        *,
        in_ticket: bool,
        image_analyses: dict[int, str] | None = None,
    ) -> list[ConversationMessage]:
        analyses = image_analyses or {}
        build = self._build_ticket_history if in_ticket else self._build_general_history
        if not isinstance(message, _AskedMessage):
            return await build(message, analyses)
        # Ask Bulma: the target is quoted in the requester's turn, which goes last (it is what they ask now); its own
        # place in the channel is skipped, so the model doesn't read it twice.
        history = await build(message, analyses, skip_id=message.id)
        analyses = {**await self._cached_image_analyses([message]), **await _log_analyses([message]), **analyses}
        asked = self._serialize_message(message, requester_id=message.author.id, image_analyses=analyses)
        if asked is not None:
            history.append(asked)
        return history

    async def _extract_image_context(self, message: discord.Message) -> dict[int, str]:
        """Analyze up to 2 new screenshots; reuse cached analyses so an image is only processed once."""
        attachments = [attachment for attachment in full_attachments(message) if _is_image_attachment(attachment)][:2]
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
                        {"type": "image", "source": {"type": "url", "url": url}},
                        {
                            "type": "text",
                            "text": (
                                "Read this support screenshot. Start with one short sentence saying "
                                "what it shows, then extract only actionable details relevant to "
                                "support: errors, warnings, version hints, symptoms, visible roles "
                                "or status, buttons clicked, and any text that changes the "
                                "recommended next step. Be concise; this text replaces the image "
                                "in the saved ticket transcript."
                            ),
                        },
                    ],
                }
            ]
            response = await asyncio.wait_for(
                vision_client.messages.create(
                    model=settings.ai_vision_model,
                    messages=payload,
                    max_tokens=settings.ai_support_max_output_tokens,
                    output_config={"effort": "low"},
                ),
                timeout=settings.ai_support_timeout_seconds,
            )
            ai_budget.record_response(response)
            text = response_text(response, "image_context").strip()
            if text:
                return text
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
        asked = isinstance(message, _AskedMessage)
        if in_ticket:
            where = "support ticket"
        elif in_dm:
            where = "direct message"
        else:
            if asked:
                how = "they used Ask Bulma on a message"
            elif self._thread_requesters.get(channel.id) == message.author.id:
                how = "you opened this thread to help them, so they needn't ping you"
            else:
                how = "you were pinged"
            if isinstance(channel, discord.Thread):
                # Members name threads (and forum posts), so the name is cleaned like a display name.
                parent = getattr(getattr(channel, "parent", None), "name", "?")
                thread_name = safe_name(getattr(channel, "name", None))
                where = f"public thread #{thread_name} in #{parent} ({how}; answer only the requester)"
            else:
                where = f"public channel #{getattr(channel, 'name', '?')} ({how}; answer only the requester)"
        name = safe_name(getattr(message.author, "display_name", message.author.name))
        lines = [f"channel: {where}", f"requester: {name} (roles: {_member_roles(member, settings)})"]
        opened = _relative_age(getattr(channel, "created_at", None)) if in_ticket else None
        if opened:
            lines.append(f"ticket opened: {opened}")
        if asked and message.target_author_id != message.author.id:
            own_answer = message.target_author_id == getattr(self.bot.user, "id", None)
            lines.append(ai_asks.asked_context_line(own_answer=own_answer))
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
        kind = _channel_kind(channel, settings=settings)
        in_ticket, in_dm = kind == "ticket", kind == "dm"
        if in_ticket and _is_staff_ticket_message(message, in_ticket=True, settings=settings):
            return
        bot_pinged = _is_pinging_bot(message, self.bot.user)
        has_content = _has_support_request_content(message, self.bot.user)
        mention_request = bot_pinged and has_content
        dm_request = in_dm and has_content
        # The member a help thread of Bulma's is for needs no ping there, as in a ticket.
        help_request = (
            kind == "public"
            and not mention_request
            and has_content
            and await self._help_thread_requester(channel) == message.author.id
        )
        public_request = mention_request or help_request

        if not in_ticket and not public_request and not dm_request:
            return
        member = (
            message.author
            if isinstance(message.author, discord.Member)
            else await self._resolve_member_for_user(message.author)
        )
        author_has_support_access = member is not None and can_use_ai_support(member, settings=settings)
        if (public_request or dm_request) and not in_ticket and not author_has_support_access:
            return

        if in_ticket and channel.id in self._escalated_ticket_channels:
            return
        has_image = any(_is_image_attachment(attachment) for attachment in full_attachments(message))
        if in_ticket or help_request or _is_reply_to_bot(message, self.bot.user):
            # Follow-ups ("still broken", "1.20.1") must reach the model; only pure acks are skipped.
            if not has_image and _is_ack_message(_strip_bot_mentions(full_content(message), self.bot.user)):
                return
        elif not _contains_log_attachment(message) and (
            _message_support_intent(message, self.bot.user) == SUPPORT_INTENT_UNCLEAR
        ):
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
        await self._answer_support(
            message, in_ticket=in_ticket, in_dm=in_dm, mention_request=public_request, member=member
        )

    async def _answer_support(
        self,
        message: discord.Message | _ChoiceMessage,
        *,
        in_ticket: bool,
        in_dm: bool,
        mention_request: bool,
        member: discord.Member | None,
    ) -> None:
        """The model's answer to message, once it passed the gates (typed messages, and answer picks on a card). A
        follow-up in a public text channel gets its answer in a new thread (_open_help_thread)."""
        channel = message.channel
        settings = self.bot.settings
        channel_kind = _channel_kind(channel, settings=settings)
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
                language = await self._ticket_language(channel.id) if in_ticket else None
                if language:
                    context_lines.append(_language_line(language))
                async with channel.typing():
                    result = await asyncio.shield(run_support_agent(
                        messages=history,
                        enabled_tools=SUPPORT_TOOL_NAMES,
                        language_hint=language,
                        user_id=message.author.id,
                        channel_id=channel.id,
                        ticket_conversation=in_ticket,
                        bot=self.bot,
                        settings=settings,
                        context_lines=context_lines,
                        requester_is_staff=member is not None and is_staff(member, settings=settings),
                        channel_kind=channel_kind,
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
            ask: dict[str, Any] | None = None
            feedback = False
            should_mark_escalated = False
            kind = result.get("kind")
            if in_ticket and _asks_for_human(full_content(message)):
                kind = "handoff"

            if in_ticket and not _is_ticket_channel(channel, settings=settings):
                return  # closed while the model was thinking
            reply_text = result["reply"].strip()
            public = not (in_ticket or in_dm)
            if public and reply_text and reply_text != "(no reply)" and not can_post_publicly(result):
                reply_text = PUBLIC_FALLBACK_TEXT
            if reply_text and reply_text != "(no reply)":
                outgoing_messages.append(reply_text)
                should_mark_escalated = in_ticket and kind == "handoff"
                if kind != "handoff" and reply_text != PUBLIC_FALLBACK_TEXT:
                    ask = result.get("ask")  # controls only under the model's own reply
                    if ask and not ai_asks.allowed_in(ask, channel_kind):
                        ask = None  # no uploads in public (moderation never sees a repost), actions only where they run
                    # 👍/👎 under a plain answer; tickets have the "solved?" prompt and staff instead.
                    feedback = not in_ticket and not ask and ai_feedback.wanted(settings, kind)
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

            async def send(where: discord.abc.Messageable) -> bool:
                return await self._send_messages_with_typing(
                    where,
                    outgoing_messages,
                    ai_answer=True,
                    ask=ask,
                    requester_id=message.author.id,
                    language=language or result.get("language"),
                    feedback=feedback,
                    response_id=result.get("response_id"),
                    reply_to=getattr(message, "reply_to", None),
                )

            # Only a real answer moves (not our fallback texts); its controls and 👍/👎 rows are saved with the thread.
            moving = public and bool(outgoing_messages) and reply_text not in ("", "(no reply)", PUBLIC_FALLBACK_TEXT)
            thread = await self._open_help_thread(message) if moving else None
            # The thread's lock: a message there waits for this answer, so its history has it.
            async with self._channel_locks[thread.id] if thread is not None else contextlib.nullcontext():
                sent = await send(thread or channel)
            if not sent and thread is not None:
                sent = await send(channel)  # the thread is there, but the answer still reaches the requester
            if sent and in_ticket and not should_mark_escalated:
                await self._awaiting_owner(channel.id)
            if sent and should_mark_escalated:
                await self._mark_ticket_escalated(channel.id)
                if kind == "handoff":
                    await self._ping_escalation_roles(channel, message.author.id)
            elif (
                sent
                and in_ticket
                and kind in (None, "answer")
                and not ask  # still asking, so not "solved?" yet
                and await self._is_ticket_owner(channel, message.author.id)
            ):
                await self._maybe_prompt_resolution(
                    channel,
                    requester_id=message.author.id,
                    confidence=result.get("confidence"),
                    force=result["suggested_close"],
                    language=language or result.get("language", "en"),
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
        self._ticket_languages[channel.id] = language
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
                        f"requester: {name} (roles: {_member_roles(member, settings)})",
                        f"ticket category: {category_label}",
                        "ticket opened: just now (the requester's intake form is the message below)",
                        _language_line(language),
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
        if not _is_ticket_channel(channel, settings=settings):
            return  # closed before the first answer was ready
        ask = result.get("ask") if result.get("kind") != "handoff" else None
        if ask and not ai_asks.allowed_in(ask, "ticket"):
            ask = None
        if not await self._send_messages_with_typing(
            channel, [reply], ai_answer=True, ask=ask, requester_id=member.id, language=language
        ):
            return
        if result.get("kind") == "handoff":
            await self._mark_ticket_escalated(channel.id)
            await self._ping_escalation_roles(channel, member.id)
        else:
            await self._awaiting_owner(channel.id)

    async def _is_ticket_owner(self, channel: discord.TextChannel, user_id: int) -> bool:
        """The tickets cog's welcome message mentions the owner; only they may get the "solved?" buttons."""
        if channel.id not in self._ticket_owners:
            owner_id = None
            async for entry in channel.history(limit=5, oldest_first=True):
                if entry.author.id == getattr(self.bot.user, "id", None):
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
        if not _is_ticket_channel(channel, settings=settings):
            return  # the ticket was closed (moved out of the open category) in the meantime
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

        try:
            self._resolve_prompts[channel.id] = await channel.send(
                view=_build_resolve_view(requester_id, language),
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=requester_id)]),
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

        # The ticket's current language, in case it changed after the prompt was posted.
        text = _resolve_text(await self._ticket_language(channel.id) or language)

        async def answer() -> bool | None:
            if solved:
                tickets = self.bot.get_cog("TicketsCog")
                if tickets is None:
                    log.warning("Tickets cog isn't loaded; can't close the solved ticket", extra={"channel_id": channel.id})
                    return False
                # Popped first, so the close (ticket_closed) doesn't delete the prompt that now says thanks.
                self._resolve_prompts.pop(channel.id, None)
                await interaction.edit_original_response(view=_card(text["thanks"], discord.Color.green()))
                await tickets.solve_ticket(channel, closer_id=user.id)
                return None
            await self._mark_ticket_escalated(channel.id)
            await self._ping_escalation_roles(channel, requester_id)
            self._resolve_prompts.pop(channel.id, None)
            await interaction.edit_original_response(view=_card(text["escalated"], discord.Color.orange()))

        await while_busy(interaction, self._working, answer, failed=RESOLVE_FAILED_TEXT)

    async def _is_closed_ticket(self, channel: Any) -> bool:
        """Closing moves a ticket out of the open category, so it looks like any channel; its tickets row tells."""
        try:
            ticket = await get_ticket_by_channel(channel.id)
        except Exception:
            log.exception("Could not look up the ticket behind an answer card", extra={"channel_id": channel.id})
            closed_category_id = self.bot.settings.ticket_closed_category_id
            return closed_category_id is not None and getattr(channel, "category_id", None) == closed_category_id
        return ticket is not None and ticket.status != STATUS_OPEN

    async def _ask_row(self, interaction: discord.Interaction, message_id: int) -> ask_store.AskRow | None:
        """The answer card's ai_asks row; None (after telling the clicker) when it is missing or unreadable."""
        try:
            row = await ask_store.get(message_id)
        except Exception:
            log.exception("Could not load answer controls", extra={"message_id": message_id})
            await interaction.response.send_message(ai_asks.text("en", "failed"), ephemeral=True)
            return None
        if row is None:
            await interaction.response.send_message(ai_asks.text("en", "expired"), ephemeral=True)
        return row

    async def _ask_refusal(
        self, interaction: discord.Interaction, row: ask_store.AskRow, *, spend: bool, ai: bool = True
    ) -> tuple[str | None, dict[str, Any]]:
        """(why this answer can't go through, as a ui/ai_asks.TEXT key, or None; the _answer_support arguments).
        The gates are the typed path's (see _process_support_message); spend: the answer runs the AI now, so it counts
        toward the rate limit. ai=False (a click that never reaches the AI: a step done, an action): only the
        requester, open-question and closed-ticket gates."""
        settings = self.bot.settings
        channel = interaction.channel
        user = interaction.user
        kind = _channel_kind(channel, settings=settings)
        in_ticket, in_dm = kind == "ticket", kind == "dm"
        # mention_request: outside tickets and DMs the card answered a ping (or its help thread's requester).
        context: dict[str, Any] = {
            "in_ticket": in_ticket, "in_dm": in_dm, "mention_request": kind == "public", "member": None
        }
        if user.id != row.requester_id:
            return "not_yours", context
        if row.answered_at is not None:
            return "answered", context
        if not in_dm and await self._is_closed_ticket(channel):
            return "closed", context
        if not ai:
            return None, context
        member = user if isinstance(user, discord.Member) else await self._resolve_member_for_user(user)
        context["member"] = member
        if (
            not settings.ai_support_enabled
            or (in_ticket and not self.is_ticket_ai_enabled(channel.id))
            or (not in_ticket and not (member is not None and can_use_ai_support(member, settings=settings)))
        ):
            return "ai_off", context
        if ai_budget.is_paused(settings):
            return "paused", context
        if spend:
            events = self._rate_limit_state.record(
                self._support_events, (user.id, 0), time.monotonic(), SUPPORT_RATE_LIMIT_WINDOW_SECONDS
            )
            if len(events) > SUPPORT_RATE_LIMIT_CALLS:
                return "rate_limited", context
        return None, context

    async def _answer_recorded(
        self,
        interaction: discord.Interaction,
        message_id: int,
        content: str,
        context: dict[str, Any],
        attachments: Sequence[Any] = (),
        card: discord.Message | None = None,
    ) -> None:
        """The answer is on the card: it is the requester's reply in the ticket, and the AI answers it like a typed
        one. card: the answer card, which a follow-up in a public text channel moves into a thread off."""
        if context["in_ticket"]:
            try:
                await note_ticket_message(interaction.channel.id, interaction.user.id)  # as TicketsCog.on_message does
            except Exception:
                log.exception("Could not track ticket reply", extra={"channel_id": interaction.channel.id})
        self._answer_pick(_ChoiceMessage(interaction, content, message_id, attachments, card=card), **context)

    async def _handle_ask(self, interaction: discord.Interaction, *, kind: str, index: int) -> None:
        """A control on an answer card (ui/ai_asks). A pick goes on the card and is answered like a typed reply;
        "Fill in" and "Upload" open their modal (submits: _handle_ask_form, _handle_ask_file); "Where is it?" only
        shows where the logs are; steps and action buttons: _handle_steps, _handle_action."""
        message = interaction.message
        if (row := await self._ask_row(interaction, message.id)) is None:
            return
        language = row.language
        if kind == "steps":
            return await self._handle_steps(interaction, row, index)
        if kind == ai_asks.ACTION_KIND:
            return await self._handle_action(interaction, row, index)
        if kind == "file" and index == ai_asks.WHERE_INDEX:
            # Just help, for anyone who opens it: it answers nothing and claims nothing.
            view = ai_asks.where_card(language, (interaction.data or {}).get("values"))
            if view is None:
                return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
            return await interaction.response.send_message(view=view, ephemeral=True)
        refusal, context = await self._ask_refusal(interaction, row, spend=kind not in ("form", "file"))
        if refusal:
            return await interaction.response.send_message(ai_asks.text(language, refusal), ephemeral=True)
        # A modal must be the first response, so no while_busy here; the submit claims the row.
        if kind == "form":
            return await interaction.response.send_modal(ai_asks.form_modal(row.spec, message.id))
        if kind == "file":
            return await interaction.response.send_modal(ai_asks.file_modal(row.spec, message.id, language))
        label = ai_asks.picked_label(row.spec, kind, index, (interaction.data or {}).get("values"))
        if not label:
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        user = interaction.user
        no_pings = discord.AllowedMentions.none()  # edits never ping; none() keeps it that way should that change

        async def pick() -> None:
            if await ask_store.claim(message.id) is None:
                # Answered meanwhile: only the controls go.
                await interaction.edit_original_response(view=_answered_card(message, []), allowed_mentions=no_pings)
                await interaction.followup.send(ai_asks.text(language, "answered"), ephemeral=True)
                return
            try:
                await interaction.edit_original_response(
                    view=_answered_card(message, [ai_asks.pick_line(user.id, label)]), allowed_mentions=no_pings
                )
            except Exception:
                await ask_store.release(message.id)
                raise
            await self._answer_recorded(interaction, message.id, label, context, card=message)

        await while_busy(interaction, self._working, pick, failed=ai_asks.text(language, "failed"))

    async def _handle_steps(self, interaction: discord.Interaction, row: ask_store.AskRow, index: int) -> None:
        """Done / Still broken on a steps card. Done before the last step moves the card on (no AI, no rate limit);
        Still broken, or Done on the last step, records the outcome and the AI answers it. The row's progress is the
        truth: a click on an outdated card only brings the card up to date."""
        message, spec, language = interaction.message, row.spec, row.language
        steps = spec.get("steps") or []
        step, button = ai_asks.split_step_index(index)
        if spec.get("kind") != "steps" or not 0 <= step < len(steps):
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        final = button == ai_asks.STEP_BROKEN or step == len(steps) - 1
        refusal, context = await self._ask_refusal(interaction, row, spend=final, ai=final)
        if refusal:
            return await interaction.response.send_message(ai_asks.text(language, refusal), ephemeral=True)
        no_pings = discord.AllowedMentions.none()
        if step != row.progress:
            await interaction.response.edit_message(view=_steps_card(message, row, row.progress), allowed_mentions=no_pings)
            return await interaction.followup.send(ai_asks.text(language, "answered"), ephemeral=True)
        failed = ai_asks.text(language, "failed")

        if not final:
            async def advance() -> bool | None:
                if not await ask_store.move(message.id, step, step + 1):
                    return False  # moved on meanwhile; the card comes back and the next click refreshes it
                try:
                    await interaction.edit_original_response(
                        view=_steps_card(message, row, step + 1), allowed_mentions=no_pings
                    )
                except Exception:
                    await ask_store.move(message.id, step + 1, step)
                    raise
                return None

            return await while_busy(interaction, self._working, advance, failed=failed)

        user = interaction.user
        if button == ai_asks.STEP_BROKEN:
            line = ai_asks.step_failed_line(user.id, step, steps[step], language)
            outcome = ai_asks.steps_text(spec, step, failed=step)
        else:
            line = ai_asks.steps_done_line(user.id, len(steps), language)
            outcome = ai_asks.steps_text(spec, len(steps))
        _, content = ai_asks.read_answer([line])  # what the model reads back, in English

        async def finish() -> None:
            if await ask_store.claim(message.id) is None:
                current = ai_asks.steps_text(spec, row.progress)
                await interaction.edit_original_response(
                    view=_answered_card(message, [], controls_text=current), allowed_mentions=no_pings
                )
                await interaction.followup.send(ai_asks.text(language, "answered"), ephemeral=True)
                return
            try:
                await interaction.edit_original_response(
                    view=_answered_card(message, [line], controls_text=outcome), allowed_mentions=no_pings
                )
            except Exception:
                await ask_store.release(message.id)
                raise
            await self._answer_recorded(interaction, message.id, content, context, card=message)

        await while_busy(interaction, self._working, finish, failed=failed)

    async def _handle_action(self, interaction: discord.Interaction, row: ask_store.AskRow, index: int) -> None:
        """An offer_action button. It runs an existing flow for the requester, never anything they couldn't start
        themselves: get_staff is the "No, I still need help" button's escalation, open_ticket the ticket panel's
        form, link_patreon /patreon link's answer, report_bug a post in the bug-report forum (its form first).
        "No thanks" only records itself. None of them runs the AI."""
        spec, language, user = row.spec, row.language, interaction.user
        action = spec.get("action")
        if spec.get("kind") != ai_asks.ACTION_KIND or index not in (ai_asks.ACTION_RUN, ai_asks.ACTION_DECLINE):
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        refusal, context = await self._ask_refusal(interaction, row, spend=False, ai=False)
        if refusal:
            return await interaction.response.send_message(ai_asks.text(language, refusal), ephemeral=True)
        if index == ai_asks.ACTION_DECLINE:
            return await self._run_action(interaction, row, ai_asks.text(language, "no_thanks"))
        if not ai_asks.allowed_in(spec, _channel_kind(interaction.channel, settings=self.bot.settings)):
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        label = ai_asks.action_label(spec, language)
        if action == "open_ticket":
            return await self._offer_ticket(interaction, row, label)
        if action == "report_bug":
            return await self._offer_bug_report(interaction, row)
        if action == "get_staff":
            channel = interaction.channel

            async def get_staff() -> list[str]:
                # AI already off: staff have the ticket (an earlier handoff, or they turned it off), so no new ping.
                if self.is_ticket_ai_enabled(channel.id):
                    await self._mark_ticket_escalated(channel.id)
                    await self._ping_escalation_roles(channel, row.requester_id)
                return [_resolve_text(await self._ticket_language(channel.id) or language)["escalated"]]

            return await self._run_action(interaction, row, label, get_staff)
        flow = self.bot.get_cog("PatreonWhitelistFlowCog")
        if flow is None:
            log.warning("Patreon cog isn't loaded; can't run a link_patreon offer", extra={"channel_id": row.channel_id})
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)

        async def link_patreon() -> list[str]:
            member = user if isinstance(user, discord.Member) else await self._resolve_member_for_user(user)
            await flow.send_link_prompt(member or user, interaction.followup)
            return []

        await self._run_action(interaction, row, label, link_patreon)

    async def _run_action(self, interaction: discord.Interaction, row: ask_store.AskRow, label: str, run=None) -> None:
        """Claims the card (the first click wins), runs run (its notes go under the answer line), then records the click
        on the card. A failure reopens the offer."""
        message, language = interaction.message, row.language
        no_pings = discord.AllowedMentions.none()

        async def work() -> None:
            if await ask_store.claim(message.id) is None:
                await interaction.edit_original_response(view=_answered_card(message, []), allowed_mentions=no_pings)
                await interaction.followup.send(ai_asks.text(language, "answered"), ephemeral=True)
                return
            try:
                notes = await run() if run is not None else []
                lines = [ai_asks.pick_line(interaction.user.id, label)]
                await interaction.edit_original_response(
                    view=_answered_card(message, lines, notes=notes), allowed_mentions=no_pings
                )
            except Exception:
                await ask_store.release(message.id)
                raise

        await while_busy(interaction, self._working, work, failed=ai_asks.text(language, "failed"))

    async def _offer_ticket(self, interaction: discord.Interaction, row: ask_store.AskRow, label: str) -> None:
        """open_ticket: the ticket panel's own form for the offered category (its category select when none was
        given), so TicketsCog.create_ticket opens the ticket as from the panel. One open ticket per member: someone
        who has one gets its link instead. The form must be the click's first response, so no while_busy here."""
        message, language, user = interaction.message, row.language, interaction.user
        if self.bot.get_cog("TicketsCog") is None:
            log.warning("Tickets cog isn't loaded; can't open a ticket from an offer", extra={"channel_id": row.channel_id})
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        try:
            open_tickets = await get_open_tickets_by_owner(user.id)
            claimed = not open_tickets and await ask_store.claim(message.id) is not None
        except Exception:
            log.exception("Could not start a ticket from an offer", extra={"message_id": message.id})
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        if open_tickets:
            return await interaction.response.send_message(msg_already_open(open_tickets[0].channel_id), ephemeral=True)
        if not claimed:
            return await interaction.response.send_message(ai_asks.text(language, "answered"), ephemeral=True)
        category = CATEGORIES.get(row.spec.get("category"))
        try:
            if category is not None:
                await interaction.response.send_modal(TicketModal(category))
            else:
                await interaction.response.send_message(view=panel_view(), ephemeral=True)
        except Exception:
            log.exception("Could not show the ticket form of an offer", extra={"message_id": message.id})
            try:
                await ask_store.release(message.id)
            except Exception:
                log.exception("Could not reopen the ticket offer", extra={"message_id": message.id})
            return
        try:
            await message.edit(
                view=_answered_card(message, [ai_asks.pick_line(user.id, label)]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Could not record a ticket offer on its card", extra={"message_id": message.id})

    async def _bug_forum(self) -> discord.ForumChannel | None:
        """The bug-report forum (bug_report_forum_channel_id); None when it isn't set or can't be found."""
        forum_id = getattr(self.bot.settings, "bug_report_forum_channel_id", None)
        if forum_id is None:
            return None
        forum = self.bot.get_channel(forum_id)
        if forum is None:
            try:
                forum = await self.bot.fetch_channel(forum_id)
            except discord.HTTPException:
                log.warning("Couldn't find the bug-report forum", exc_info=True, extra={"channel_id": forum_id})
                return None
        return forum if isinstance(forum, discord.ForumChannel) else None

    async def _bug_report_target(self, user: discord.abc.User) -> tuple[Any, discord.ForumChannel] | None:
        """(BugReportsCog, the forum) when the requester can report a bug there: the forum is set and found, the cog is
        loaded, and they are in the forum's server (the post credits them, and its triage card's "Open a ticket" needs
        them there). None otherwise."""
        bugs = self.bot.get_cog(BUG_REPORTS_COG)
        if not getattr(self.bot.settings, "bug_reports_enabled", False):
            bugs = None  # off: nothing would triage the post
        forum = await self._bug_forum() if bugs is not None else None
        if forum is None:
            return None
        guild = forum.guild
        if isinstance(user, discord.Member) and user.guild.id == guild.id:
            return bugs, forum
        if guild.get_member(user.id) is None:
            try:
                await guild.fetch_member(user.id)
            except discord.NotFound:
                return None
            except discord.HTTPException:
                log.info("Couldn't look up a bug reporter", exc_info=True, extra={"user_id": user.id})
                return None
        return bugs, forum

    async def _offer_bug_report(self, interaction: discord.Interaction, row: ask_store.AskRow) -> None:
        """report_bug: the bug report form, pre-filled with what the model passed. Nothing is claimed yet: the submit
        (_handle_ask_bug) claims the card, so a closed form leaves the offer open. Refused at once when the report
        couldn't be posted (_bug_report_target). The form must be the click's first response, so no while_busy here."""
        language = row.language
        if await self._bug_report_target(interaction.user) is None:
            return await interaction.response.send_message(ai_asks.text(language, "bug_closed"), ephemeral=True)
        try:
            await interaction.response.send_modal(ai_asks.bug_modal(row.spec, interaction.message.id, language))
        except Exception:
            log.exception("Could not show the bug report form", extra={"message_id": interaction.message.id})

    async def _handle_ask_bug(self, interaction: discord.Interaction, message_id: int) -> None:
        """A submitted report_bug form. The first submit claims the card; Bulma posts the report in the bug-report
        forum (it credits the requester and carries their recent logs and screenshots from this conversation), the
        card records the post's link, and BugReportsCog triages it with the requester as reporter. If the post can't
        be made the offer is open again."""
        if (row := await self._ask_row(interaction, message_id)) is None:
            return
        spec, language, user = row.spec, row.language, interaction.user
        failed = ai_asks.text(language, "failed")
        if spec.get("action") != "report_bug" or not ai_asks.allowed_in(
            spec, _channel_kind(interaction.channel, settings=self.bot.settings)
        ):
            return await interaction.response.send_message(failed, ephemeral=True)
        refusal, _context = await self._ask_refusal(interaction, row, spend=False, ai=False)
        if refusal:
            return await interaction.response.send_message(ai_asks.text(language, refusal), ephemeral=True)
        answers = ai_asks.bug_answers((interaction.data or {}).get("components"))
        if not all(answers.get(field) for field in ai_asks.BUG_REQUIRED):
            return await interaction.response.send_message(failed, ephemeral=True)
        if (target := await self._bug_report_target(user)) is None:
            return await interaction.response.send_message(ai_asks.text(language, "bug_closed"), ephemeral=True)
        bugs, forum = target
        try:
            claimed = await ask_store.claim(message_id) is not None
        except Exception:
            log.exception("Could not claim a bug report", extra={"message_id": message_id})
            return await interaction.response.send_message(failed, ephemeral=True)
        if not claimed:
            return await interaction.response.send_message(ai_asks.text(language, "answered"), ephemeral=True)
        # Downloading the files and posting can outlast the 3-second window; invisible=False keeps it ephemeral.
        await interaction.response.defer(ephemeral=True, invisible=False)
        no_pings = discord.AllowedMentions.none()
        body = ai_asks.bug_post(user.id, answers)
        try:
            files = await self._reporter_files(interaction.channel, message_id, user.id, forum.guild.filesize_limit)
            thread = await forum.create_thread(
                name=_thread_name(answers["title"], self.bot.settings) or BUG_POST_FALLBACK_NAME,
                content=body,
                files=files,
                allowed_mentions=no_pings,
            )
        except Exception:
            log.exception("Could not post a bug report", extra={"message_id": message_id})
            try:
                await ask_store.release(message_id)
            except Exception:
                log.exception("Could not reopen the bug report offer", extra={"message_id": message_id})
            return await interaction.followup.send(failed, ephemeral=True)
        link = f"https://discord.com/channels/{forum.guild.id}/{thread.id}"
        try:
            message = interaction.message
            if getattr(message, "id", None) != message_id:  # a modal submit may come without its message
                message = await interaction.channel.fetch_message(message_id)
            line = ai_asks.bug_line(user.id, link, language)
            await message.edit(view=_answered_card(message, [line]), allowed_mentions=no_pings)
        except Exception:
            # The post is up, so the card stays claimed (reopening it would let a second report through).
            log.exception("Could not record a bug report on its card", extra={"message_id": message_id})
        try:
            await interaction.followup.send(ai_asks.text(language, "bug_done").format(link=link), ephemeral=True)
        except discord.HTTPException:
            log.info("Couldn't confirm a bug report", exc_info=True, extra={"message_id": message_id})
        await self._triage_bug_post(bugs, thread, body, user.id)

    async def _reporter_files(
        self, channel: Any, card_id: int, requester_id: int, size_limit: int
    ) -> list[discord.File]:
        """The requester's own logs (.log/.txt) and screenshots from the conversation the answer card belongs to,
        newest first, at most BUG_REPORT_MAX_FILES within size_limit together, re-uploaded: their messages' files and
        the files Bulma reposted for them (upload cards). The conversation is what the answer read: a thread's
        messages (_thread_context), elsewhere the public context window before the card."""
        try:
            if isinstance(channel, discord.Thread):
                messages = await self._thread_context(channel)
            else:
                cutoff = discord.utils.snowflake_time(card_id) - GENERAL_CONTEXT_MAX_AGE
                messages = [
                    entry
                    async for entry in channel.history(
                        limit=GENERAL_CONTEXT_MESSAGE_LIMIT, before=discord.Object(id=card_id)
                    )
                    if entry.created_at >= cutoff
                ]
                messages.reverse()
        except discord.HTTPException:
            log.info("Couldn't read the conversation for a bug report", exc_info=True, extra={"channel_id": channel.id})
            return []
        picked: list[discord.Attachment] = []
        budget = size_limit  # one post carries them all, so the limit is for the total
        for attachment in (item for entry in reversed(messages) for item in self._own_files(entry, requester_id)):
            if len(picked) >= BUG_REPORT_MAX_FILES:
                break
            size = attachment.size or 0
            if (
                any(attachment.id == seen.id for seen in picked)
                or not (_is_log_attachment(attachment) or _is_image_attachment(attachment))
                or size > budget
            ):
                continue
            picked.append(attachment)
            budget -= size
        files: list[discord.File] = []
        for attachment, name in zip(picked, ai_asks.upload_names([attachment.filename for attachment in picked])):
            try:
                files.append(discord.File(io.BytesIO(await attachment.read()), filename=name))
            except discord.HTTPException:
                log.info("Couldn't copy a file into a bug report", exc_info=True, extra={"attachment_id": attachment.id})
        return files

    def _own_files(self, entry: discord.Message, requester_id: int) -> Sequence[Any]:
        """The files that are the requester's in this message: their own message's, or Bulma's repost of their upload."""
        author = entry.author
        if author.id == requester_id and not author.bot:
            return full_attachments(entry)
        if author.id == getattr(self.bot.user, "id", None):
            upload = ai_asks.read_upload(_card_texts(entry))
            if upload is not None and upload[0] == requester_id:
                return entry.attachments
        return ()

    async def _triage_bug_post(self, bugs: Any, thread: discord.Thread, body: str, reporter_id: int) -> None:
        """BugReportsCog's triage of a post Bulma made for the requester, with them as reporter (its listener leaves
        posts whose first message is a bot's alone). Off with the rest of the triage (bug_reports_enabled)."""
        if not getattr(self.bot.settings, "bug_reports_enabled", False):
            return
        try:
            starter = await bugs._fetch_starter_message(thread)
            await bugs.triage_thread(
                thread,
                text=f"{thread.name}\n\n{body}".strip(),
                reporter_id=reporter_id,
                attachments=list(starter.attachments) if starter is not None else [],
            )
        except Exception:
            log.exception("Could not triage a bug report Bulma posted", extra={"channel_id": thread.id})

    async def _handle_ask_form(self, interaction: discord.Interaction, message_id: int) -> None:
        """A submitted "Fill in" form: one line per answered field goes on the card, then the AI answers them."""
        if (row := await self._ask_row(interaction, message_id)) is None:
            return
        language = row.language
        refusal, context = await self._ask_refusal(interaction, row, spend=True)
        if refusal:
            return await interaction.response.send_message(ai_asks.text(language, refusal), ephemeral=True)
        answers = ai_asks.form_answers(row.spec, (interaction.data or {}).get("components"))
        try:
            claimed = bool(answers) and await ask_store.claim(message_id) is not None
        except Exception:
            log.exception("Could not claim a form answer", extra={"message_id": message_id})
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        if not claimed:
            reason = "answered" if answers else "failed"
            return await interaction.response.send_message(ai_asks.text(language, reason), ephemeral=True)
        await interaction.response.defer(invisible=True)
        try:
            message = interaction.message
            if getattr(message, "id", None) != message_id:  # a modal submit may come without its message
                message = await interaction.channel.fetch_message(message_id)
            lines = ai_asks.form_lines(interaction.user.id, answers, used=sum(map(len, _card_texts(message))))
            await message.edit(view=_answered_card(message, lines), allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            log.exception("Could not record a form answer on its card", extra={"message_id": message_id})
            try:
                await ask_store.release(message_id)
            except Exception:
                log.exception("Could not reopen the form", extra={"message_id": message_id})
            return await interaction.followup.send(ai_asks.text(language, "failed"), ephemeral=True)
        content = "\n".join(f"{label}: {value}" for label, value in answers)
        await self._answer_recorded(interaction, message_id, content, context, card=message)

    async def _handle_ask_file(self, interaction: discord.Interaction, message_id: int) -> None:
        """A submitted "Upload": the files are reposted on a card credited to the requester (history and transcripts
        read it as their turn), the question card records them, logs get their analysis card, then the AI answers
        (screenshots analyzed on the way), so its history already holds the analyses."""
        if (row := await self._ask_row(interaction, message_id)) is None:
            return
        language = row.language
        refusal, context = await self._ask_refusal(interaction, row, spend=True)
        if refusal:
            return await interaction.response.send_message(ai_asks.text(language, refusal), ephemeral=True)
        try:
            state = getattr(interaction, "_state", None)
            uploads = [discord.Attachment(data=data, state=state) for data in ai_asks.uploaded(row.spec, interaction.data)]
        except Exception:
            log.exception("Could not read an upload", extra={"message_id": message_id})
            uploads = []
        if not uploads:
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        if sum(upload.size or 0 for upload in uploads) > _upload_limit(interaction):  # one message carries them all
            return await interaction.response.send_message(ai_asks.text(language, "too_big"), ephemeral=True)
        try:
            claimed = await ask_store.claim(message_id) is not None
        except Exception:
            log.exception("Could not claim an upload", extra={"message_id": message_id})
            return await interaction.response.send_message(ai_asks.text(language, "failed"), ephemeral=True)
        if not claimed:
            return await interaction.response.send_message(ai_asks.text(language, "answered"), ephemeral=True)
        await interaction.response.defer(invisible=True)
        user = interaction.user
        no_pings = discord.AllowedMentions.none()
        names = ai_asks.upload_names([upload.filename for upload in uploads])
        try:
            files = [
                discord.File(io.BytesIO(await upload.read()), filename=name) for upload, name in zip(uploads, names)
            ]
            view = ai_asks.upload_card(user.id, names, [_is_image_attachment(u) for u in uploads], AI_CARD_COLOR)
            reposted = await interaction.channel.send(view=view, files=files, allowed_mentions=no_pings)
        except Exception:
            log.exception("Could not repost an upload", extra={"message_id": message_id})
            try:
                await ask_store.release(message_id)
            except Exception:
                log.exception("Could not reopen the upload", extra={"message_id": message_id})
            return await interaction.followup.send(ai_asks.text(language, "failed"), ephemeral=True)
        try:
            message = interaction.message
            if getattr(message, "id", None) != message_id:  # a modal submit may come without its message
                message = await interaction.channel.fetch_message(message_id)
            lines = [ai_asks.upload_line(user.id, name) for name in names]
            await message.edit(view=_answered_card(message, lines), allowed_mentions=no_pings)
        except Exception:
            # The repost is the record the AI and transcripts read; the files are in, so the answer goes on.
            log.exception("Could not record an upload on its card", extra={"message_id": message_id})
        await self._show_upload_cards(reposted, user)
        content = "\n".join(f"uploaded {name}" for name in names)
        await self._answer_recorded(interaction, message_id, content, context, attachments=reposted.attachments)

    async def _show_upload_cards(self, message: discord.Message, uploader: discord.abc.User) -> None:
        """The log / dmzdebug cards for a reposted upload, as a typed upload gets them (not in DMs, like those).
        LogParserCog.parse_upload never adds its AI diagnosis, since the support AI answers next. Failures never stop
        the answer."""
        if getattr(message, "guild", None) is None:
            return
        for cog_name, post in (
            ("LogParserCog", lambda cog: cog.parse_upload(message, uploader)),
            ("DmzDebugCog", lambda cog: cog.post_cards(message)),
        ):
            if (cog := self.bot.get_cog(cog_name)) is None:
                continue
            try:
                await post(cog)
            except Exception:
                log.exception("Could not analyze a reposted upload", extra={"message_id": message.id, "cog": cog_name})

    async def _handle_feedback(self, interaction: discord.Interaction, requester_id: int, helpful: bool) -> None:
        """👍/👎 under an answer (ui/ai_feedback): only the requester's first rating counts; the card then swaps the
        buttons for a thanks line in the answer's language."""
        message = interaction.message
        try:
            row = await feedback_store.get(message.id)
        except Exception:
            log.exception("Could not load answer feedback", extra={"message_id": message.id})
            return await interaction.response.send_message(ai_asks.text("en", "failed"), ephemeral=True)
        language = row.language if row is not None else "en"
        if interaction.user.id != requester_id:
            return await interaction.response.send_message(ai_feedback.text(language, "not_yours"), ephemeral=True)

        async def rate() -> None:
            # A second rating (a race, or a click after a failed edit) changes nothing; the card still says thanks.
            await feedback_store.rate(
                message_id=message.id, channel_id=message.channel.id, requester_id=requester_id, helpful=helpful
            )
            await interaction.edit_original_response(
                view=_rebuilt(message, discord.ui.TextDisplay(ai_feedback.text(language, "thanks"))),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await while_busy(interaction, self._working, rate, failed=ai_asks.text(language, "failed"))

    def _answer_pick(self, message: _ChoiceMessage, *, in_ticket: bool, **kwargs: Any) -> None:
        """Answers in the background, so a failing AI run never brings the buttons back (it reports its own errors).
        It is the pending answer of the ticket (or of the member elsewhere), as a typed message's is: a newer message,
        an escalation or a close cancels it. It stays keyed by the card's channel when the answer moves into a new
        thread: the thread only exists once the answer is ready, and a message there waits on the thread's lock until
        the answer is in (_answer_support)."""
        pending_key = _pending_key(message, in_ticket=in_ticket)
        self._cancel_pending_task(pending_key)

        async def answer() -> None:
            try:
                await self._answer_support(message, in_ticket=in_ticket, **kwargs)
            except asyncio.CancelledError:
                return
            finally:
                if self._pending_tasks.get(pending_key) is asyncio.current_task():
                    self._pending_tasks.pop(pending_key, None)

        self._pending_tasks[pending_key] = asyncio.create_task(answer())

    async def _maybe_close_solved(self, channel: discord.TextChannel, message: discord.Message) -> None:
        """The AI is off here; the owner saying it's solved closes the ticket like the "Yes" button
        (TicketsCog.solve_ticket)."""
        settings = self.bot.settings
        if channel.id in self._closing_channels or not message.content.strip() or ai_budget.is_paused(settings):
            return
        if not await self._is_ticket_owner(channel, message.author.id):
            return
        bot_id = getattr(self.bot.user, "id", None)
        recent = [entry async for entry in channel.history(limit=SOLVED_CHECK_CONTEXT_MESSAGES, before=message)]
        conversation = "\n".join(
            f"{'Owner' if entry.author.id == message.author.id else 'Bot' if entry.author.id == bot_id else 'Staff'}: "
            f"{entry.content[:500]}"
            for entry in [*reversed(recent), message]
            if entry.content
        )
        try:
            solved = await owner_says_solved(
                conversation,
                model=settings.ai_ticket_summary_model,
                timeout_seconds=settings.ai_support_timeout_seconds,
            )
        except Exception:
            log.exception("Ticket solved check failed", extra={"channel_id": channel.id})
            return
        if not solved or channel.id in self._closing_channels:
            return
        tickets = self.bot.get_cog("TicketsCog")
        if tickets is None:
            return log.warning("Tickets cog isn't loaded; can't close the solved ticket", extra={"channel_id": channel.id})
        log.info("Ticket owner said it's solved, closing", extra={"channel_id": channel.id})
        try:
            language = await self._ticket_language(channel.id) or "en"
            await channel.send(view=_card(SOLVED_CLOSE_TEXT.get(language, SOLVED_CLOSE_TEXT["en"]), discord.Color.green()))
        except discord.HTTPException:
            log.info("Could not post solved note in ticket", extra={"channel_id": channel.id})
        await tickets.solve_ticket(channel, closer_id=message.author.id)

    async def _collect_transcript(
        self,
        channel: discord.TextChannel,
        requester_id: int | None,
    ) -> tuple[list[TranscriptLine], int | None]:
        history = [
            entry
            async for entry in channel.history(limit=TRANSCRIPT_MESSAGE_LIMIT, oldest_first=True)
        ]
        # Messages sent while the ticket was closed stay out; without the tickets cog nothing records those stretches.
        tickets = self.bot.get_cog("TicketsCog")
        spans = await tickets.closed_spans_for(channel.id) if tickets is not None else []
        entries = hide_closed_messages(history, spans, bot_id=getattr(self.bot.user, "id", None))
        messages = [entry for entry in entries if not isinstance(entry, ClosedGap)]
        if requester_id is None:
            # Our welcome message mentions the owner; fall back to the first non-staff human.
            requester_id = next(
                (
                    mentioned.id
                    for entry in messages
                    if entry.author.id == getattr(self.bot.user, "id", None)
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
        for entry in entries:
            if isinstance(entry, ClosedGap):
                lines.append(TranscriptLine(entry.created_at, "system", "System", entry.notice))
                continue
            if entry.author.bot and entry.author != self.bot.user:
                continue
            if (uploaded := self._upload_turn(entry, requester_id, analyses)) is not None:
                lines.append(
                    TranscriptLine(
                        entry.created_at, uploaded["speaker_kind"], uploaded["speaker_name"], uploaded["content"]
                    )
                )
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
            if (picked := self._choice_turn(entry, requester_id)) is not None:
                lines.append(
                    TranscriptLine(
                        getattr(entry, "edited_at", None) or entry.created_at,
                        picked["speaker_kind"],
                        picked["speaker_name"],
                        picked["content"],
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

    async def _suggest_support_info(
        self, channel_id: int, summary: TicketSummary, *, ticket_id: int | None, page_link: str | None
    ) -> None:
        """Hands the summary's support info suggestion to SupportInfoCog for staff review; never breaks the close."""
        support_cog = self.bot.get_cog("SupportInfoCog")
        if support_cog is None:
            return
        try:
            await support_cog.suggest_from_ticket(summary.support_info, ticket_id=ticket_id, page_link=page_link)
        except Exception:
            log.exception("Failed to suggest support info", extra={"channel_id": channel_id})

    async def _close_ticket(
        self,
        channel: discord.TextChannel,
        *,
        closed_by_id: int | None,
        requester_id: int | None,
        resolved: bool | None,
        staff_solved: bool = False,
        ticket_id: int | None = None,
    ) -> bool:
        """TicketsCog's archive step: transcript → hosted HTML page → summarize → learn (knowledge_docs) → record.
        resolved=None lets the summary decide. staff_solved: closed with the staff Solved button; then a support info
        suggestion from the summary goes to staff review.
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
            message_count = sum(line.speaker_kind != "system" for line in lines)
            transcript = render_transcript_text(lines, channel_name=channel.name)
            page = await self._build_page(channel)

            summary: TicketSummary | None = None
            try:
                if not ai_budget.is_paused(settings):
                    summary = await summarize_ticket(
                        lines,
                        model=settings.ai_ticket_summary_model,
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

            knowledge_filename: str | None = None
            # Only tickets a staff member took part in may teach the AI, and only their lines are kept.
            staff_reviewed = any(line.speaker_kind == "staff" for line in lines)
            if summary is not None and summary.knowledge_worthy and staff_reviewed:
                try:
                    # A re-close overwrites the doc an earlier close wrote: one knowledge doc per ticket.
                    filename = await stored_knowledge_filename(channel.id) or ticket_knowledge_filename(
                        channel.id, closed_at
                    )
                    knowledge_filename = await save_ticket_knowledge(
                        render_knowledge_markdown(summary, lines, closed_at=closed_at),
                        filename=filename,
                        title=summary.title,
                    )
                except Exception:
                    log.exception("Failed to save ticket knowledge", extra={"channel_id": channel.id})
            # The Solved click is staff vouching for the ticket, and staff approve the suggestion before it is used.
            if staff_solved and summary is not None and summary.support_info is not None:
                await self._suggest_support_info(
                    channel.id, summary, ticket_id=ticket_id, page_link=page_url(settings, page.token) if page else None
                )

            confidence = self._last_confidence.pop(channel.id, None)
            try:
                await record_ticket_transcript(
                    channel_id=channel.id,
                    guild_id=channel.guild.id,
                    channel_name=channel.name,
                    requester_id=requester_id,
                    closed_by_id=closed_by_id,
                    resolved=resolved,
                    ai_confidence=confidence,
                    message_count=message_count,
                    summary=summary,
                    transcript=transcript,
                    knowledge_filename=knowledge_filename,
                    html_token=page.token if page else None,
                    html_expires_at=page.expires_at if page else None,
                )
                await delete_image_analyses(channel.id)
                archived = True  # the panel's transcript row is the archive now
            except Exception:
                log.exception("Failed to record ticket transcript", extra={"channel_id": channel.id})

            self._archived_channels.add(channel.id)
            self._resolve_prompts.pop(channel.id, None)
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
        custom_id = (interaction.data or {}).get("custom_id", "")
        if interaction.type == discord.InteractionType.modal_submit:
            if (form_message_id := ai_asks.parse_form_modal_id(custom_id)) is not None:
                await self._handle_ask_form(interaction, form_message_id)
            elif (file_message_id := ai_asks.parse_file_modal_id(custom_id)) is not None:
                await self._handle_ask_file(interaction, file_message_id)
            elif (bug_message_id := ai_asks.parse_bug_modal_id(custom_id)) is not None:
                await self._handle_ask_bug(interaction, bug_message_id)
            return
        if interaction.type != discord.InteractionType.component:
            return
        if (ask := ai_asks.parse_custom_id(custom_id)) is not None:
            kind, _requester_id, index = ask  # the row says whose question it is
            return await self._handle_ask(interaction, kind=kind, index=index)
        if (rating := ai_feedback.parse_custom_id(custom_id)) is not None:
            return await self._handle_feedback(interaction, *rating)
        parsed = _parse_resolve_custom_id(custom_id)
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
        if not settings.ai_support_enabled:
            return

        channel = message.channel
        if isinstance(channel, discord.DMChannel):
            if message.author.bot:
                return
            if full_content(message).startswith(("!", "/", ".")):
                return
            if not await self._can_use_support_from_message(message, settings=settings):
                return
            self._schedule_support_response(message, in_ticket=False)
            return

        if not message.guild or not (
            isinstance(channel, discord.TextChannel) or _is_support_thread(channel, settings=settings)
        ):
            return

        in_ticket = _channel_kind(channel, settings=settings) == "ticket"
        bot_pinged = _is_pinging_bot(message, self.bot.user)
        mention_request = bot_pinged and _has_support_request_content(message, self.bot.user)
        # In a help thread of Bulma's, the member it is for needs no ping; everyone else still pings her.
        help_request = (
            not (in_ticket or mention_request or message.author.bot)
            and await self._help_thread_requester(channel) == message.author.id
        )
        author_has_support_access = await self._can_use_support_from_message(message, settings=settings)
        if not in_ticket and not mention_request and not help_request:
            return
        if (mention_request or help_request) and not in_ticket and not author_has_support_access:
            return

        if not message.author.bot and isinstance(message.author, discord.Member):
            self._cancel_pending_task_for_message(message, in_ticket=in_ticket)

        if message.author.bot or not isinstance(message.author, discord.Member):
            return
        if _is_staff_ticket_message(message, in_ticket=in_ticket, settings=settings):
            return
        if in_ticket and channel.id in self._escalated_ticket_channels:
            await self._maybe_close_solved(channel, message)
            return
        if full_content(message).startswith(("!", "/", ".")) and not mention_request:
            return
        self._schedule_support_response(message, in_ticket=in_ticket)


def setup(bot: discord.Bot):
    bot.add_cog(AITicketsCog(bot))
