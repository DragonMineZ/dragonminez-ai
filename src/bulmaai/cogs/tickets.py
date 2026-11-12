"""In-house ticket system: dropdown panel → intake modal → private channel → close (moves to the closed category).
Every close archives a hosted HTML transcript; a channel keeps one address across re-open and re-close.

AI answers inside tickets still come from AITicketsCog (same category); this cog owns the lifecycle.
"""

import asyncio
import base64
import html as html_lib
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from enum import IntEnum

import chat_exporter
import discord
from chat_exporter import AttachmentHandler
from discord.ext import commands, tasks

from bulmaai.services import ai_budget
from bulmaai.services.ai_guard import BLOCKING_FLAGS, defuse_mentions, link_targets, screen_reply
from bulmaai.services.staff_copilot import LANGUAGE_NAMES, translate_owner_text
from bulmaai.services.ticket_intake import TicketIntake, triage_intake
from bulmaai.services.ticket_pages import StoredPage, get_page_for_channel, page_url, purge, save_page
from bulmaai.services.ticket_transcripts import SupportInfoSuggestion
from bulmaai.services.tickets import (
    STATUS_CLOSED,
    STATUS_OPEN,
    ClosedGap,
    Span,
    Ticket,
    abandon_ticket,
    attach_channel,
    cancel_delete,
    claim_owner_reminders,
    closed_channel_name,
    closed_span_index,
    closed_spans,
    get_open_tickets_by_owner,
    get_ticket_by_channel,
    get_ticket_by_id,
    hide_closed_messages,
    list_active_tickets,
    list_due_solved,
    list_inactive_tickets,
    mark_closed,
    mark_deleted,
    mark_reopened,
    note_ticket_message,
    open_channel_name,
    reserve_ticket,
    schedule_delete,
    set_language,
    set_transcript_resolved,
    take_support_suggestion,
)
from bulmaai.services.patron_page import PATREON_URL
from bulmaai.ui import staff_copilot
from bulmaai.ui.ticket_views import (
    ACTIONS_ID,
    AI_BUTTON_ID,
    AUTOCLOSE_BUTTON_ID,
    CATEGORIES,
    CLOSE_BUTTON_ID,
    DELETE_BUTTON_ID,
    FORM_BUTTON_PREFIX,
    LANG_BUTTON_PREFIX,
    LANG_SELECT_ID,
    LANGUAGES,
    PANEL_SELECT_ID,
    REOPEN_BUTTON_ID,
    SOLVED_REOPEN_PREFIX,
    SUMMARY_BUTTON_ID,
    TRANSCRIPT_BUTTON_ID,
    CLOSED_RED,
    ClosePrompt,
    TicketCategory,
    TicketModal,
    closed_row,
    closed_view,
    control_row,
    deleting_notice,
    dm_assigned,
    dm_created,
    dm_reminder,
    dm_solved,
    dm_transcript,
    form_language_card,
    language_changed,
    language_set,
    msg_created,
    msg_already_open,
    notice,
    panel_view,
    parse_form_custom_id,
    relanguage_welcome,
    reopened_view,
    request_text,
    t,
    transcript_link_view,
    welcome_view,
)
from bulmaai.ui.v2 import already_working, unbusy, while_busy
from bulmaai.utils.forwards import inline_forwards
from bulmaai.utils.i18n import described, reader_language, table, user_language
from bulmaai.utils.language import detect_language_from_text, role_languages
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import is_admin, is_bruno

log = logging.getLogger(__name__)

AI_COG = "AITicketsCog"
# ponytail: newest 1000 messages, same cap as the AI transcript; page through history if tickets outgrow it.
TRANSCRIPT_MESSAGE_LIMIT = 1000
# Hosted pages live on our disk, so this is only a sanity cap; Discord's upload limit matters just for /ticket transcript.
PAGE_MAX_BYTES = 25_000_000
# Images are inlined so the archived .html outlives Discord's expiring attachment links.
INLINE_IMAGE_LIMIT = 1_500_000
INLINE_TOTAL_LIMIT = 5_000_000
RENAME_TIMEOUT_SECONDS = 10
REASON_LEFT = "User left the server."
# A ticket closed as solved is deleted this long after, unless staff re-open it first.
SOLVED_DELETE_AFTER = timedelta(hours=1)
# Every deletion is announced in the channel this long before it happens.
DELETE_NOTICE_SECONDS = 10
# The owner gets one DM after this long without replying to someone else's answer in their ticket.
OWNER_REMINDER_HOURS = 12
# Still no reply this long after that DM: the bot closes the ticket (auto-close on).
INACTIVE_CLOSE_HOURS = 24
REASON_INACTIVE = "💤 Closed for inactivity: no reply within 24h of the reminder."
REASON_SOLVED = "✅ Solved."
# chat_exporter's <head> puts raw HTML (emoji <img>) into the link-preview meta tags; ours replaces it.
_HEAD_META = re.compile(r"<title>.*?(?=<style>)", re.S)
_DONATE = re.compile(r'<a href="https://ko-fi\.com/mahtoid">DONATE</a>')
# The chat log's closing tag in chat_exporter's page, right after the last message group.
_CHATLOG_END = re.compile(r'</div>\s*</div>\s*<div class="footer">')
_CLOSED_NOTICE = '<div style="margin: 0 0 1rem 72px; color: #a3a6aa; font-size: 0.85rem; font-style: italic">{}</div>'


# /ticket descriptions, per Discord client language (command names stay English).
DESC = table({
    "en": {
        "ticket": "Ticket tools",
        "panel": "Post the ticket panel in a channel",
        "panel.channel": "Where to post it (default: here)",
        "add": "Let a user or role see this ticket",
        "add.target": "User or role to add",
        "assign": "Assign this ticket to a member",
        "assign.member": "Member who will handle it",
        "remove": "Hide this ticket from a user or role",
        "remove.target": "User or role to remove",
        "close": "Close this ticket",
        "close.reason": "Why it's being closed",
        "transcript": "Export this ticket's transcript as HTML",
    },
    "es": {
        "ticket": "Herramientas de tickets",
        "panel": "Publica el panel de tickets en un canal",
        "panel.channel": "Dónde publicarlo (por defecto: aquí)",
        "add": "Deja que un usuario o rol vea este ticket",
        "add.target": "Usuario o rol a añadir",
        "assign": "Asigna este ticket a un miembro",
        "assign.member": "Miembro que se encargará",
        "remove": "Oculta este ticket a un usuario o rol",
        "remove.target": "Usuario o rol a quitar",
        "close": "Cierra este ticket",
        "close.reason": "Por qué se cierra",
        "transcript": "Exporta la transcripción de este ticket como HTML",
    },
    "pt": {
        "ticket": "Ferramentas de tickets",
        "panel": "Publica o painel de tickets em um canal",
        "panel.channel": "Onde publicar (padrão: aqui)",
        "add": "Deixa um usuário ou cargo ver este ticket",
        "add.target": "Usuário ou cargo a adicionar",
        "assign": "Atribui este ticket a um membro",
        "assign.member": "Membro que vai cuidar dele",
        "remove": "Esconde este ticket de um usuário ou cargo",
        "remove.target": "Usuário ou cargo a remover",
        "close": "Fecha este ticket",
        "close.reason": "Por que está sendo fechado",
        "transcript": "Exporta a transcrição deste ticket em HTML",
    },
})


class Rank(IntEnum):
    NONE = 0
    OWNER = 1
    HELPER = 2
    MOD = 3


def _ids(*groups) -> set[int]:
    return {int(role_id) for group in groups for role_id in group}


def member_rank(member: discord.abc.User, owner_id: int | None, settings) -> Rank:
    """Highest hat wins."""
    if is_bruno(member):
        return Rank.MOD
    role_ids = {role.id for role in getattr(member, "roles", [])}
    if role_ids & _ids(settings.panel_owner_role_ids, settings.panel_admin_role_ids, settings.panel_moderator_role_ids):
        return Rank.MOD
    if role_ids & _ids(settings.panel_helper_role_ids):
        return Rank.HELPER
    if member.id == owner_id:
        return Rank.OWNER
    return Rank.NONE


def is_mod_only(category_key: str | None) -> bool:
    """Tickets of a mod-only category (Report a Member, Appeal a Punishment) are for moderators and up only."""
    category = CATEGORIES.get(category_key or "")
    return category is not None and category.mod_only


def build_overwrites(
    guild: discord.Guild, owner: discord.Member | None, settings, *, closed: bool = False, mod_only: bool = False
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """closed: staff only, so the owner and anyone /ticket add-ed lose the channel. mod_only: Helpers get no access
    (is_mod_only), only moderators and up."""
    mods = _ids(settings.panel_owner_role_ids, settings.panel_admin_role_ids, settings.panel_moderator_role_ids)
    helpers = set() if mod_only else _ids(settings.panel_helper_role_ids) - mods

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
            manage_channels=True,
            manage_messages=True,
        ),
    }
    if owner is not None and not closed:
        overwrites[owner] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )
    staff_kwargs = dict(view_channel=True, send_messages=True, read_message_history=True)
    for role_ids, overwrite in (
        (helpers, discord.PermissionOverwrite(manage_messages=False, **staff_kwargs)),
        (mods, discord.PermissionOverwrite(manage_messages=True, **staff_kwargs)),
    ):
        for role_id in role_ids:
            if (role := guild.get_role(role_id)) is not None:
                overwrites[role] = overwrite
    return overwrites


def polish_transcript(html: str, *, title: str, description: str, image: str | None) -> str:
    """Clean link preview + our Patreon instead of chat_exporter's donate link."""
    esc = lambda text: html_lib.escape(text, quote=True)
    meta = [
        f"<title>{esc(title)}</title>",
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />',
        '<meta name="viewport" content="width=device-width" />',
        '<meta name="theme-color" content="#5865f2" />',
        f'<meta name="description" content="{esc(description)}" />',
        '<meta property="og:type" content="website" />',
        '<meta property="og:site_name" content="DragonMine Z Tickets" />',
        f'<meta property="og:title" content="{esc(title)}" />',
        f'<meta property="og:description" content="{esc(description)}" />',
    ]
    if image:
        meta.append(f'<meta property="og:image" content="{esc(image)}" />')
    html = _HEAD_META.sub(lambda _: "\n    ".join(meta) + "\n    ", html, count=1)
    return _DONATE.sub(lambda _: f'<a href="{esc(PATREON_URL)}">PATREON</a>', html, count=1)


def add_closed_notices(html: str, entries: list) -> str:
    """hide_closed_messages' gaps become notices before the next message shown (ahead of its message group when it
    starts one), or at the end of the chat log."""
    pending = ""
    for entry in [*entries, None]:
        if isinstance(entry, ClosedGap):
            pending += _CLOSED_NOTICE.format(html_lib.escape(entry.notice))
        elif pending:
            anchor = _CHATLOG_END if entry is None else re.compile(
                rf'(?:<div class="chatlog__message-group">\s*)?<div id="chatlog__message-container-{entry.id}"'
            )
            if spots := list(anchor.finditer(html)):
                html = html[: spots[-1].start()] + pending + html[spots[-1].start() :]
            pending = ""
    return html


class InlineImageHandler(AttachmentHandler):
    """Swaps small image attachments for data URIs, within a total budget."""

    def __init__(self, budget: int = INLINE_TOTAL_LIMIT) -> None:
        self.budget = budget

    async def process_asset(self, attachment: discord.Attachment) -> discord.Attachment:
        content_type = (attachment.content_type or "").split(";")[0].strip().lower()
        if (
            not content_type.startswith("image/")
            or content_type == "image/svg+xml"
            or attachment.size > min(INLINE_IMAGE_LIMIT, self.budget)
        ):
            return attachment
        try:
            data = await attachment.read()
        except discord.HTTPException:
            return attachment
        self.budget -= len(data)
        attachment.url = attachment.proxy_url = f"data:{content_type};base64,{base64.b64encode(data).decode()}"
        return attachment


class TicketsCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        # ponytail: in-memory guard against double-clicks; one bot process owns the guild.
        self._deleting: set[int] = set()
        self._working: set[int] = set()  # messages whose button click is still being handled (ui.v2.while_busy)
        self._copilot_working: set[tuple[int, int]] = set()  # (ticket channel, staff) with a Summary/Draft running

    @property
    def settings(self):
        return self.bot.settings

    async def on_startup(self) -> None:
        for loop in (self._purge_pages, self._delete_solved, self._remind_owners):
            if not loop.is_running():
                loop.start()
        try:
            # Channels deleted while the bot was offline would otherwise count against their owner's limit.
            for ticket in await list_active_tickets():
                if self.bot.get_channel(ticket.channel_id) is None:
                    await mark_deleted(ticket.channel_id)
        except Exception:
            log.exception("Failed to reconcile ticket channels")

    async def on_shutdown(self) -> None:
        self._purge_pages.cancel()
        self._delete_solved.cancel()
        self._remind_owners.cancel()

    @tasks.loop(hours=1)
    async def _purge_pages(self) -> None:
        try:
            removed = await purge(self.settings)
        except Exception:
            log.exception("Transcript purge failed")
            return
        if removed:
            log.info("Purged %d expired or orphaned ticket transcript files", removed)

    @tasks.loop(minutes=1)
    async def _delete_solved(self) -> None:
        """Closed tickets past delete_at: solved ones whose re-open window is over, and deletions a restart cut short.
        The deadline lives in the DB, so restarts don't lose it."""
        try:
            due = await list_due_solved()
        except Exception:
            return log.exception("Could not list solved tickets to delete")
        # Each deletion waits out its notice; side by side, so a backlog doesn't queue them.
        await asyncio.gather(*(self._sweep_delete(ticket) for ticket in due))

    async def _sweep_delete(self, ticket: Ticket) -> None:
        try:
            channel = self.bot.get_channel(ticket.channel_id)
            if isinstance(channel, discord.TextChannel):
                await self.delete_ticket(channel, ticket)
            else:
                await mark_deleted(ticket.channel_id)
        except Exception:
            log.exception("Failed to delete solved ticket", extra={"ticket_id": ticket.ticket_id})

    @tasks.loop(minutes=10)
    async def _remind_owners(self) -> None:
        """DMs owners who left an answer in their ticket unreplied for OWNER_REMINDER_HOURS (once per silence)."""
        try:
            due = await claim_owner_reminders(OWNER_REMINDER_HOURS)
        except Exception:
            return log.exception("Could not claim ticket owner reminders")
        for ticket in due:
            channel = self.bot.get_channel(ticket.channel_id)
            if not self._automated(ticket, channel):
                continue
            owner = channel.guild.get_member(ticket.owner_id)
            if owner is None:
                continue
            try:
                await owner.send(view=dm_reminder(ticket.ticket_id, channel, ticket.language))
            except discord.HTTPException:
                log.info("Could not DM ticket reminder", extra={"user_id": ticket.owner_id})
        await self._close_inactive()

    async def _close_inactive(self) -> None:
        """Open tickets whose owner still hasn't replied INACTIVE_CLOSE_HOURS after the reminder: closed by the bot with
        auto-close on (deleted SOLVED_DELETE_AFTER later unless staff re-open it); the summary decides whether it was
        solved, and the owner gets the usual transcript DM. The state is all in the DB (mark_closed closes once)."""
        try:
            due = await list_inactive_tickets(INACTIVE_CLOSE_HOURS)
        except Exception:
            return log.exception("Could not list inactive tickets")
        for ticket in due:
            channel = self.bot.get_channel(ticket.channel_id)
            try:
                if not isinstance(channel, discord.TextChannel):
                    await mark_deleted(ticket.channel_id)
                    continue
                if not self._automated(ticket, channel):
                    continue
                delete_at = datetime.now(timezone.utc) + SOLVED_DELETE_AFTER
                await self.close_ticket(channel, closer_id=None, reason=REASON_INACTIVE, delete_at=delete_at)
            except Exception:
                log.exception("Failed to close inactive ticket", extra={"ticket_id": ticket.ticket_id})

    def _automated(self, ticket: Ticket, channel) -> bool:
        """Reminders and inactivity closes only touch ticket channels still in the open-tickets category (staff may
        have moved one out to keep it) and never hands-off categories (Contributing)."""
        category = CATEGORIES.get(ticket.category)
        return (
            isinstance(channel, discord.TextChannel)
            and channel.category_id == self.settings.ai_ticket_category_id
            and not (category is not None and category.hands_off)
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id") or ""
        if interaction.type != discord.InteractionType.component:
            return
        elif custom_id.startswith(SOLVED_REOPEN_PREFIX):
            handler = self.on_solved_reopen
        elif custom_id.startswith(LANG_BUTTON_PREFIX):
            handler = self.on_language
        elif custom_id.startswith(FORM_BUTTON_PREFIX):
            handler = self.on_form_language
        else:
            handler = {
                PANEL_SELECT_ID: self.on_panel_select,
                LANG_SELECT_ID: self.on_language,
                CLOSE_BUTTON_ID: self.on_close,
                AI_BUTTON_ID: self.on_toggle_ai,
                TRANSCRIPT_BUTTON_ID: self.on_transcript,
                REOPEN_BUTTON_ID: self.on_reopen,
                DELETE_BUTTON_ID: self.on_delete,
                AUTOCLOSE_BUTTON_ID: self.on_autoclose,
                SUMMARY_BUTTON_ID: self.on_summary,
            }.get(custom_id)
        if handler is None:
            return
        try:
            await handler(interaction)
        except Exception:
            log.exception("Ticket interaction failed", extra={"channel_id": interaction.channel_id})
            if not interaction.response.is_done():
                await interaction.response.send_message(t(self._lang(interaction), "failed"), ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        try:
            await mark_deleted(channel.id)
        except Exception:
            log.exception("Failed to mark ticket channel deleted", extra={"channel_id": channel.id})

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Every human message in an open ticket feeds the owner's reply countdown (staff pings are automod's staff_ping)."""
        settings = self.settings
        if (
            message.author.bot
            or not isinstance(message.author, discord.Member)
            or getattr(message.channel, "category_id", None) != settings.ai_ticket_category_id
        ):
            return
        try:
            await note_ticket_message(message.channel.id, message.author.id)
        except Exception:
            log.exception("Could not track ticket reply", extra={"channel_id": message.channel.id})

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            tickets = await get_open_tickets_by_owner(member.id)
        except Exception:
            log.exception("Failed to look up tickets of departed member", extra={"user_id": member.id})
            return
        for ticket in tickets:
            channel = member.guild.get_channel(ticket.channel_id)
            if ticket.guild_id != member.guild.id or not isinstance(channel, discord.TextChannel):
                continue
            closed = await self.close_ticket(channel, closer_id=None, reason=REASON_LEFT)
            if closed is not None:
                await self.delete_ticket(channel, closed)

    # ---- ticket creation -------------------------------------------------------------------------

    async def _already_open(self, owner_id: int, language: str = "en") -> str:
        """One open ticket per member: point them at it (it has no channel yet while still being created)."""
        open_tickets = await get_open_tickets_by_owner(owner_id)
        return msg_already_open(open_tickets[0].channel_id if open_tickets else None, language)

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        category: TicketCategory,
        answers: list[tuple[str, str]],
        *,
        owner: discord.Member | None = None,
        files: list[discord.File] | None = None,
        language: str | None = None,
    ) -> discord.TextChannel | None:
        """owner: open it for someone else (staff acting for a bug reporter). files go in right after the welcome,
        before the AI reads the ticket. language: the form's language (the panel's: their one language role or their
        pick), which is the ticket's support language; without it, a single language role decides, else the form's
        text (the welcome card's select switches it). Returns the channel, or None after telling the clicker why
        not (in language, else the clicker's role language)."""
        guild, member, settings = interaction.guild, owner or interaction.user, self.settings
        # invisible=False: a modal submit's default ("invisible") defer would edit the public panel message instead.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, invisible=False)
        if guild is None or not isinstance(member, discord.Member):
            return None
        reply_language = language or user_language(interaction.user, settings, interaction)
        await interaction.edit_original_response(content=t(reply_language, "creating"))

        form_text = "\n".join(f"{label}: {value}" for label, value in answers)
        role_langs = [] if language else role_languages(member, settings)
        if language is None and len(role_langs) == 1:
            language = role_langs[0]
        ticket = await reserve_ticket(
            guild_id=guild.id,
            owner_id=member.id,
            category=category.key,
            language=language or detect_language_from_text(form_text),
        )
        if ticket is None:
            await interaction.edit_original_response(content=await self._already_open(member.id, reply_language))
            return None

        channel: discord.TextChannel | None = None
        try:
            intake, translation = await asyncio.gather(
                self._triage(category, answers, ticket), self._translate_request(answers, ticket)
            )
            language = language or intake.language
            parent = guild.get_channel(settings.ai_ticket_category_id) if settings.ai_ticket_category_id else None
            if not isinstance(parent, discord.CategoryChannel):
                raise RuntimeError("ai_ticket_category_id is not a category channel")
            name = open_channel_name(intake.channel_slug, ticket.ticket_id)
            channel = await guild.create_text_channel(
                name,
                category=parent,
                overwrites=build_overwrites(guild, member, settings, mod_only=category.mod_only),
                topic=f"Ticket #{ticket.ticket_id:04d} · {category.key} · owner {member.id}",
                reason=f"Ticket #{ticket.ticket_id:04d} opened",
            )
            welcome = await channel.send(
                view=welcome_view(
                    number=ticket.ticket_id,
                    category=category,
                    answers=answers,
                    mention=member.mention,
                    language=language,
                    opened_at=ticket.created_at or datetime.now(timezone.utc),
                    avatar_url=member.display_avatar.url,
                    translation=translation,
                ),
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
            await attach_channel(
                ticket.ticket_id,
                channel_id=channel.id,
                channel_name=name,
                language=language,
                control_message_id=welcome.id,
            )
        except Exception:
            log.exception("Failed to create ticket", extra={"ticket_id": ticket.ticket_id, "user_id": member.id})
            if channel is not None:
                try:
                    await channel.delete(reason="Ticket creation failed")
                except discord.HTTPException:
                    pass
            await abandon_ticket(ticket.ticket_id)
            await interaction.edit_original_response(content=t(reply_language, "failed"))
            return None

        await self._pin_welcome(channel, welcome)
        if files:
            try:
                await channel.send(files=files)
            except discord.HTTPException:
                log.warning("Could not post carried-over files", extra={"ticket_id": ticket.ticket_id})
        await interaction.edit_original_response(content=msg_created(channel.mention, reply_language))
        try:
            await member.send(view=dm_created(ticket.ticket_id, category, channel, language))
        except discord.HTTPException:
            pass  # DMs closed; the ephemeral message above already has the link

        ai_cog = self.bot.get_cog(AI_COG)
        if ai_cog is not None and (category.mod_only or category.hands_off):
            # moderators or the team handle these; staff may turn it back on
            await ai_cog.set_ticket_ai_enabled(channel.id, False)
        if ai_cog is not None and category.ai_reply:
            await ai_cog.answer_new_ticket(
                channel, member, category_label=category.name, form_text=form_text, language=language
            )
        return channel

    async def _pin_welcome(self, channel: discord.TextChannel, welcome: discord.Message) -> None:
        """Pins the welcome card, then removes the "pinned a message" line Discord posts for it. Best effort."""
        try:
            await welcome.pin(reason="Ticket welcome card")
        except Exception:  # never breaks the ticket that is already open
            return log.warning("Could not pin the ticket welcome card", extra={"channel_id": channel.id})
        bot_id = getattr(self.bot.user, "id", None)
        try:
            async for message in channel.history(limit=5):
                if message.type == discord.MessageType.pins_add and message.author.id == bot_id:
                    await message.delete()
                    break
        except Exception:
            log.info("Could not remove the pin notice", extra={"channel_id": channel.id})

    async def _translate_request(self, answers: list[tuple[str, str]], ticket: Ticket) -> str | None:
        """The request card's answers in English for staff (one Haiku call), when the ticket's language at reservation
        isn't English and the AI isn't paused. Screened like a staff translation; any failure means no translation."""
        settings = self.settings
        source = ticket.language
        if not answers or source == "en" or source not in LANGUAGE_NAMES or ai_budget.is_paused(settings):
            return None
        text = request_text(answers)
        try:
            translations = await translate_owner_text(
                text,
                source,
                ["en"],
                model=settings.ai_translation_model,
                timeout_seconds=settings.ai_support_timeout_seconds,
            )
        except Exception:
            log.exception("Ticket request translation failed", extra={"ticket_id": ticket.ticket_id})
            return None
        translation = (translations.get("en") or "").strip()
        if not translation:
            return None
        # The member's own links are theirs to keep, so they count as ours (as for in-ticket translations).
        screened, flags = screen_reply(translation, settings, link_targets(text))
        if flags & BLOCKING_FLAGS or not screened.strip():
            return None
        return defuse_mentions(screened.strip())

    async def _triage(self, category: TicketCategory, answers: list[tuple[str, str]], ticket: Ticket) -> TicketIntake:
        """AI picks the channel name and language; any failure just falls back to the category defaults."""
        settings = self.settings
        fallback = TicketIntake(channel_slug=category.fallback_slug, language=ticket.language, summary="")
        if ai_budget.is_paused(settings):
            return fallback
        try:
            return await triage_intake(
                category.name,
                answers,
                model=settings.ai_ticket_summary_model,
                fallback_slug=category.fallback_slug,
            )
        except Exception:
            log.exception("Ticket intake triage failed", extra={"ticket_id": ticket.ticket_id})
            return fallback

    # ---- close / reopen ---------------------------------------------------------------------------

    async def close_ticket(
        self,
        channel: discord.TextChannel,
        *,
        closer_id: int | None,
        reason: str | None,
        resolved: bool | None = None,
        delete_at: datetime | None = None,
        dm: bool = True,
    ) -> Ticket | None:
        """Staff-only overwrites, move to the closed category, rename, post the closed card, then archive the
        transcript (see archive). None if it wasn't open. delete_at: closed with auto-close on (solved by the owner,
        or for inactivity); the closed card shows it and its toggle starts On."""
        ticket = await mark_closed(channel.id, closed_by=closer_id, reason=reason, delete_at=delete_at)
        if ticket is None:
            return None
        if (ai_cog := self.bot.get_cog(AI_COG)) is not None:
            await ai_cog.ticket_closed(channel.id)  # drops an answer or "solved?" prompt still in flight
        await self._safely(
            channel.edit(
                overwrites=build_overwrites(
                    channel.guild, None, self.settings, closed=True, mod_only=is_mod_only(ticket.category)
                ),
                reason="Ticket closed",
            ),
            channel,
        )
        await self._move(channel, self.settings.ticket_closed_category_id)
        await self._rename(channel, closed_channel_name(ticket.ticket_id))
        await self._set_buttons(channel, ticket, None)
        card = await self._safely(
            channel.send(view=self._closed_card(ticket), allowed_mentions=discord.AllowedMentions.none()),
            channel,
        )
        # Re-Open/Delete on the card answer "Already working on it." until the transcript is saved.
        if card is not None:
            self._working.add(card.id)
        try:
            await self.archive(channel, ticket, closed_by_id=closer_id, card=card, resolved=resolved, dm=dm)
        finally:
            if card is not None:
                self._working.discard(card.id)
        return ticket

    @staticmethod
    def _closed_card(ticket: Ticket, url: str | None = None, *, saving: bool = True) -> discord.ui.DesignerView:
        """The closed card, built from the ticket row (closer, reason, auto-close deadline)."""
        return closed_view(
            f"<@{ticket.closed_by}>" if ticket.closed_by else None,
            ticket.close_reason and defuse_mentions(ticket.close_reason),
            delete_at=ticket.delete_at,
            url=url,
            saving=saving,
        )

    async def reopen_ticket(self, channel: discord.TextChannel, *, opener_id: int) -> Ticket | None:
        ticket = await mark_reopened(channel.id)
        if ticket is None:
            return None
        if (ai_cog := self.bot.get_cog(AI_COG)) is not None:
            ai_cog.forget_archived(channel.id)
        # ponytail: back to the creation overwrites; anyone /ticket add-ed before the close needs adding again.
        owner = channel.guild.get_member(ticket.owner_id)
        await self._safely(
            channel.edit(
                overwrites=build_overwrites(channel.guild, owner, self.settings, mod_only=is_mod_only(ticket.category)),
                reason="Ticket re-opened",
            ),
            channel,
        )
        await self._move(channel, self.settings.ai_ticket_category_id)
        await self._rename(channel, ticket.channel_name or open_channel_name("ticket", ticket.ticket_id))
        await self._set_buttons(channel, ticket, control_row(self._ai_enabled(channel.id), ticket.language))
        await self._safely(
            channel.send(
                view=reopened_view(f"<@{opener_id}>", ticket.language), allowed_mentions=discord.AllowedMentions.none()
            ),
            channel,
        )
        return ticket

    async def _safely(self, coro, channel: discord.TextChannel):
        """The coroutine's result, or None (logged) if Discord refused it."""
        try:
            return await coro
        except discord.HTTPException:
            log.exception("Ticket channel update failed", extra={"channel_id": channel.id})

    async def _rename(self, channel: discord.TextChannel, name: str) -> None:
        # Discord allows 2 renames per 10 minutes; past that the library sleeps, so don't wait for it.
        try:
            await asyncio.wait_for(channel.edit(name=name), RENAME_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, discord.HTTPException):
            log.warning("Could not rename ticket channel", extra={"channel_id": channel.id, "name": name})

    async def _move(self, channel: discord.TextChannel, category_id: int | None) -> None:
        category = channel.guild.get_channel(category_id) if category_id else None
        if isinstance(category, discord.CategoryChannel) and channel.category_id != category.id:
            # Overwrites stay as they are (sync_permissions defaults to False).
            await self._safely(channel.edit(category=category), channel)

    async def _set_buttons(self, channel: discord.TextChannel, ticket: Ticket, row: discord.ui.ActionRow | None) -> None:
        if ticket.control_message_id is None:
            return
        try:
            message = await channel.fetch_message(ticket.control_message_id)
        except discord.HTTPException:
            return log.warning("Ticket welcome message is gone", extra={"channel_id": channel.id})
        await self._set_row(message, row)

    async def _set_row(self, message: discord.Message, row: discord.ui.ActionRow | None) -> None:
        """Swap a card's button row (ACTIONS_ID) for row, or drop it."""
        view = discord.ui.DesignerView.from_message(message, timeout=None)
        if view.get_item(ACTIONS_ID) is not None:
            view.remove_item(ACTIONS_ID)
        if row is not None:
            view.add_item(row)
        await self._safely(message.edit(view=view, allowed_mentions=discord.AllowedMentions.none()), message.channel)

    # ---- button handlers (called from ui/ticket_views.py) ----------------------------------------

    async def _gate(
        self, interaction: discord.Interaction, minimum: Rank, *, denied: str = "no_permission", want_open: bool | None = True
    ) -> Ticket | None:
        """Ticket for this channel if the clicker may act on it; otherwise answers ephemerally and returns None."""
        ticket = await get_ticket_by_channel(interaction.channel_id) if interaction.channel_id else None
        if ticket is None:
            await interaction.response.send_message(t(self._lang(interaction), "not_ticket"), ephemeral=True)
            return None
        if member_rank(interaction.user, ticket.owner_id, self.settings) < minimum:
            await interaction.response.send_message(t(self._lang(interaction), denied), ephemeral=True)
            return None
        if want_open is not None and (ticket.status == STATUS_OPEN) != want_open:
            key = "already_closed" if want_open else "not_closed"
            await interaction.response.send_message(t(self._lang(interaction), key), ephemeral=True)
            return None
        return ticket

    async def on_panel_select(self, interaction: discord.Interaction) -> None:
        """A category picked on the panel: a member with one language role gets its form in that language at once;
        anyone else first picks a language (form_language_card, then on_form_language)."""
        category = CATEGORIES.get(((interaction.data or {}).get("values") or [""])[0])
        languages = role_languages(interaction.user, self.settings)
        if category is None:
            return await interaction.response.send_message(t(self._lang(interaction), "failed"), ephemeral=True)
        if len(languages) == 1:
            await interaction.response.send_modal(TicketModal(category, languages[0]))
        else:
            await interaction.response.send_message(view=form_language_card(category), ephemeral=True)
        # An ephemeral copy (Bulma's open_ticket offer) can't be edited through the channel, and only its viewer sees it.
        message = interaction.message
        if message is not None and not getattr(getattr(message, "flags", None), "ephemeral", False):
            # Re-sending the card clears the option the dropdown still shows as picked (and brings old panels up to
            # date with the current categories).
            await self._safely(message.edit(view=panel_view()), interaction.channel)

    async def on_form_language(self, interaction: discord.Interaction) -> None:
        """A language button of form_language_card: that category's form in that language. The custom id carries
        both, so it works after a restart. The modal must be the click's first response."""
        parsed = parse_form_custom_id((interaction.data or {}).get("custom_id") or "")
        if parsed is None:
            return await interaction.response.send_message(t(self._lang(interaction), "failed"), ephemeral=True)
        category, language = parsed
        await interaction.response.send_modal(TicketModal(category, language))

    def _lang(self, interaction) -> str:
        """The language to answer whoever triggered interaction (or a command's ctx) in."""
        return reader_language(interaction, self.settings)

    def _reply_language(self, interaction, ticket: Ticket) -> str:
        """The owner reads the ticket's language; anyone else (staff) their own (reader_language)."""
        return ticket.language if interaction.user.id == ticket.owner_id else self._lang(interaction)

    async def on_close(self, interaction: discord.Interaction) -> None:
        ticket = await self._gate(interaction, Rank.OWNER)
        if ticket is None:
            return
        prompt = ClosePrompt(self._close_from_prompt, self._reply_language(interaction, ticket))
        await interaction.response.send_message(view=prompt, ephemeral=True)

    async def _close_from_prompt(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        ticket = await self.close_ticket(channel, closer_id=interaction.user.id, reason=None)
        page = await get_page_for_channel(channel.id) if ticket is not None else None
        if page is not None:
            # Also shown here: the owner can't see the channel any more and their DMs may be closed.
            language = self._reply_language(interaction, ticket)
            link = transcript_link_view(ticket.ticket_id, page_url(self.settings, page.token), language)
            await interaction.edit_original_response(view=link)
        else:
            text = t(self._lang(interaction), "already_closed" if ticket is None else "transcript_failed")
            await interaction.edit_original_response(view=notice(text, CLOSED_RED))

    def _ai_enabled(self, channel_id: int) -> bool:
        ai = self.bot.get_cog("AITicketsCog")
        return ai is None or ai.is_ticket_ai_enabled(channel_id)

    async def on_toggle_ai(self, interaction: discord.Interaction) -> None:
        if await self._gate(interaction, Rank.HELPER, denied="staff_only") is None:
            return
        ai = self.bot.get_cog("AITicketsCog")
        if ai is None:
            return await interaction.response.send_message(t(self._lang(interaction), "failed"), ephemeral=True)

        async def toggle() -> None:
            enable = not ai.is_ticket_ai_enabled(interaction.channel_id)
            await ai.set_ticket_ai_enabled(interaction.channel_id, enable)  # also flips the button (refresh_ai_button)
            await interaction.followup.send(
                f"🤖 AI support is now **{'on' if enable else 'off'}** (by {interaction.user.mention}).",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await while_busy(interaction, self._working, toggle, failed=t(self._lang(interaction), "failed"))

    async def refresh_ai_button(self, channel_id: int, enabled: bool) -> None:
        """Called by the AI tickets cog whenever AI support flips, by hand or by escalation."""
        ticket = await get_ticket_by_channel(channel_id)
        channel = self.bot.get_channel(channel_id)
        if ticket is not None and ticket.status == STATUS_OPEN and isinstance(channel, discord.TextChannel):
            await self._set_buttons(channel, ticket, control_row(enabled, ticket.language))

    async def on_language(self, interaction: discord.Interaction) -> None:
        """The welcome card's language select or a new-ticket picker: owner or staff set the support language.
        Pickers are no longer posted; the button path stays for ones already in open tickets, whose first AI answer
        waits for the pick."""
        data = interaction.data or {}
        picker = data.get("custom_id") != LANG_SELECT_ID
        language = data["custom_id"].removeprefix(LANG_BUTTON_PREFIX) if picker else (data.get("values") or [""])[0]
        if language not in LANGUAGES:
            return await interaction.response.send_message(t(self._lang(interaction), "failed"), ephemeral=True)
        ticket = await self._gate(interaction, Rank.OWNER)
        if ticket is None:
            return
        await interaction.response.defer()
        if not picker and language == ticket.language:
            return
        ticket = await set_language(interaction.channel_id, language)
        if ticket is None:  # closed meanwhile
            return await interaction.followup.send(t(self._lang(interaction), "already_closed"), ephemeral=True)
        if (ai_cog := self.bot.get_cog(AI_COG)) is not None:
            ai_cog.forget_ticket_language(interaction.channel_id)
        channel = interaction.channel
        welcome = await self._relanguage_welcome(channel, ticket, None if picker else interaction.message)
        if picker:  # the picker itself becomes the public confirmation
            await interaction.edit_original_response(view=language_set(language))
            category = CATEGORIES.get(ticket.category)
            if ai_cog is not None and welcome is not None and category is not None and category.ai_reply:
                # The first answer waited for this pick; the request is read back off the welcome card.
                await ai_cog.answer_picked_ticket(
                    channel,
                    ticket.owner_id,
                    category_label=category.name,
                    form_text=welcome.children[1].items[0].content,
                    language=language,
                )
            return
        await self._safely(
            channel.send(
                view=language_changed(language, interaction.user.mention), allowed_mentions=discord.AllowedMentions.none()
            ),
            channel,
        )

    async def _relanguage_welcome(
        self, channel: discord.TextChannel, ticket: Ticket, message: discord.Message | None = None
    ) -> discord.ui.DesignerView | None:
        """The welcome card's greeting and language select follow the ticket's language. message: the welcome card,
        when the caller has it already. Returns the card as rewritten, None when it's gone."""
        category = CATEGORIES.get(ticket.category)
        if category is None or ticket.control_message_id is None:
            return
        if message is None:
            try:
                message = await channel.fetch_message(ticket.control_message_id)
            except discord.HTTPException:
                return log.warning("Ticket welcome message is gone", extra={"channel_id": channel.id})
        view = relanguage_welcome(
            discord.ui.DesignerView.from_message(message, timeout=None),
            number=ticket.ticket_id,
            category=category,
            mention=f"<@{ticket.owner_id}>",
            language=ticket.language,
        )
        await self._safely(message.edit(view=view, allowed_mentions=discord.AllowedMentions.none()), channel)
        return view

    async def on_transcript(self, interaction: discord.Interaction) -> None:
        """Retry on a closed card whose archive failed, and the Transcript button old closed cards still carry:
        re-archives into the channel's same address, then the card shows the link."""
        ticket = await self._gate(interaction, Rank.HELPER, denied="staff_only", want_open=None)
        if ticket is None:
            return

        async def retry() -> bool:
            # archive turns the card's transcript slot into the link (or Retry again)
            page = await self.archive(
                interaction.channel, ticket, closed_by_id=ticket.closed_by or interaction.user.id, card=interaction.message
            )
            return page is not None

        await while_busy(interaction, self._working, retry, failed=t(self._lang(interaction), "transcript_failed"))

    async def on_reopen(self, interaction: discord.Interaction) -> None:
        ticket = await self._gate(interaction, Rank.MOD, denied="mod_only", want_open=False)
        if ticket is None:
            return
        if interaction.guild.get_member(ticket.owner_id) is None:
            return await interaction.response.send_message(t(self._lang(interaction), "owner_left"), ephemeral=True)

        async def reopen() -> None:
            await self.reopen_ticket(interaction.channel, opener_id=interaction.user.id)
            await self._set_row(interaction.message, None)  # this closed card's buttons are spent

        await while_busy(interaction, self._working, reopen, failed=t(self._lang(interaction), "failed"))

    async def on_delete(self, interaction: discord.Interaction) -> None:
        """Mods delete any closed ticket; helpers only ones with auto-close on (skips the re-open wait)."""
        ticket = await self._gate(interaction, Rank.HELPER, denied="mod_only", want_open=False)
        if ticket is None:
            return
        if ticket.delete_at is None and member_rank(interaction.user, ticket.owner_id, self.settings) < Rank.MOD:
            return await interaction.response.send_message(t(self._lang(interaction), "mod_only"), ephemeral=True)

        async def noticed() -> None:
            # The deletion notice is the answer; the card's buttons come back so a Re-Open within it still wins.
            self._working.discard(interaction.message.id)
            await unbusy(interaction)

        async def delete() -> bool:
            return await self.delete_ticket(interaction.channel, ticket, on_notice=noticed)

        await while_busy(interaction, self._working, delete, failed=t(self._lang(interaction), "delete_failed"))

    async def on_autoclose(self, interaction: discord.Interaction) -> None:
        """The closed card's Auto-close toggle (staff). On: the ticket is solved; it is deleted SOLVED_DELETE_AFTER
        later unless re-opened, its transcript is marked solved, and the close's support info suggestion goes to staff
        review (once). Off: no deadline, transcript not solved. The card is rebuilt from the ticket row and a short
        public notice says who flipped it."""
        ticket = await self._gate(interaction, Rank.HELPER, denied="staff_only", want_open=False)
        if ticket is None:
            return
        channel = interaction.channel
        if channel.id in self._deleting:  # a deletion notice is running; Re-Open is what stops it
            return await interaction.response.send_message(already_working(self._lang(interaction)), ephemeral=True)

        async def toggle() -> bool:
            current = await get_ticket_by_channel(channel.id)
            if current is None or current.status != STATUS_CLOSED:
                return False
            if current.delete_at is None:
                updated = await schedule_delete(channel.id, datetime.now(timezone.utc) + SOLVED_DELETE_AFTER)
            else:
                updated = await cancel_delete(channel.id)
            if updated is None:
                return False
            on = updated.delete_at is not None
            try:
                await set_transcript_resolved(channel.id, on)
            except Exception:
                log.exception("Could not mark the ticket transcript", extra={"channel_id": channel.id})
            if on:
                await self._suggest_support_info(channel, updated)
            page = await get_page_for_channel(channel.id)
            url = page_url(self.settings, page.token) if page else None
            await interaction.edit_original_response(view=self._closed_card(updated, url, saving=False))
            state = (
                f"on by <@{interaction.user.id}>: deletes <t:{int(updated.delete_at.timestamp())}:R>."
                if on
                else f"off by <@{interaction.user.id}>."
            )
            await self._safely(
                channel.send(view=notice(f"⏱️ Auto-close {state}"), allowed_mentions=discord.AllowedMentions.none()),
                channel,
            )
            return True

        await while_busy(interaction, self._working, toggle, failed=t(self._lang(interaction), "failed"))

    async def _suggest_support_info(self, channel: discord.TextChannel, ticket: Ticket) -> None:
        """A staff member vouched for the ticket as solved: the close summary's stored suggestion (if any) goes to
        SupportInfoCog's review, and is cleared so it goes only once. Never breaks the caller."""
        support_cog = self.bot.get_cog("SupportInfoCog")
        if support_cog is None:
            return
        try:
            stored = await take_support_suggestion(channel.id)
            if stored is None:
                return
            suggestion = SupportInfoSuggestion(str(stored["kind"]), str(stored["title"]), str(stored["body"]))
            page = await get_page_for_channel(channel.id)
            await support_cog.suggest_from_ticket(
                suggestion,
                ticket_id=ticket.ticket_id,
                page_link=page_url(self.settings, page.token) if page else None,
            )
        except Exception:
            log.exception("Failed to suggest support info", extra={"channel_id": channel.id})

    async def solve_ticket(self, channel: discord.TextChannel, *, closer_id: int, staff: bool = False) -> Ticket | None:
        """Closes as solved: the owner confirming it to the AI (AITicketsCog), or a staff member through Staff Bulma
        (staff, which also sends the support info suggestion to review, as the Auto-close toggle does). Archived as
        resolved with auto-close on, the owner gets the solved DM, and the sweep (_delete_solved) deletes it
        SOLVED_DELETE_AFTER later unless staff re-open it. None if it wasn't open."""
        delete_at = datetime.now(timezone.utc) + SOLVED_DELETE_AFTER
        ticket = await self.close_ticket(
            channel, closer_id=closer_id, reason=REASON_SOLVED, resolved=True, delete_at=delete_at, dm=False
        )
        if ticket is not None:
            if staff:
                await self._suggest_support_info(channel, ticket)
            await self._dm_solved(channel, ticket, delete_at)
        return ticket

    async def _dm_solved(self, channel: discord.TextChannel, ticket: Ticket, delete_at: datetime) -> None:
        """The solved DM (in place of the transcript one); staff hear about it in the channel if it can't be sent."""
        try:
            page = await get_page_for_channel(channel.id)
        except Exception:
            log.exception("Could not look up hosted transcript", extra={"channel_id": channel.id})
            page = None
        owner = channel.guild.get_member(ticket.owner_id)
        if owner is not None:
            url = page_url(self.settings, page.token) if page else None
            view = dm_solved(ticket.ticket_id, channel.guild, url, page and page.expires_at, delete_at, ticket.language)
            try:
                await owner.send(view=view)
                return
            except discord.HTTPException:
                log.info("Could not DM solved ticket", extra={"user_id": ticket.owner_id})
        await self._safely(
            channel.send(
                view=notice(f"⚠️ I couldn't DM <@{ticket.owner_id}> that their ticket was solved (DMs closed or they left)."),
                allowed_mentions=discord.AllowedMentions.none(),
            ),
            channel,
        )

    async def on_solved_reopen(self, interaction: discord.Interaction) -> None:
        """The Re-open button older solved DMs still carry: owners can't re-open any more, staff do it in the channel."""
        ticket_id = int(interaction.data["custom_id"].removeprefix(SOLVED_REOPEN_PREFIX))
        ticket = await get_ticket_by_id(ticket_id)
        language = ticket.language if ticket is not None else "en"
        await interaction.response.send_message(t(language, "reopen_staff"), ephemeral=True)

    # ---- staff copilot (ui/staff_copilot; the AI work is AITicketsCog's) --------------------------------

    async def on_summary(self, interaction: discord.Interaction) -> None:
        """📋 Summary: the ticket so far, for the staff member who clicked."""

        async def summary(ai, ticket: Ticket) -> discord.ui.DesignerView | None:
            so_far = await ai.ticket_summary(interaction.channel, ticket)
            if so_far is None:
                return None
            return staff_copilot.summary_card(ticket.ticket_id, so_far, self._lang(interaction))

        await self._copilot(interaction, summary)

    async def _copilot(self, interaction: discord.Interaction, build) -> None:
        """Summary: staff only, while the ticket is open and the AI budget isn't paused. The answer is an ephemeral card
        for the clicker (build(ai cog, ticket), None when there is nothing to show). The click gets an ephemeral
        "thinking" at once instead of while_busy, which would lock the whole welcome card (the owner's Close) while the
        AI works; a second click by the same staff member on this ticket meanwhile is refused."""
        ticket = await self._gate(interaction, Rank.HELPER, denied="staff_only")
        if ticket is None:
            return
        ai = self.bot.get_cog(AI_COG)
        if ai is None:
            return await interaction.response.send_message(t(self._lang(interaction), "failed"), ephemeral=True)
        if ai_budget.is_paused(self.settings):
            return await interaction.response.send_message(t(self._lang(interaction), "ai_paused"), ephemeral=True)
        key = (interaction.channel_id, interaction.user.id)
        if key in self._copilot_working:
            return await interaction.response.send_message(already_working(self._lang(interaction)), ephemeral=True)
        self._copilot_working.add(key)
        try:
            await interaction.response.defer(ephemeral=True, invisible=False)
            try:
                view = await build(ai, ticket)
            except Exception:
                log.exception("Staff copilot failed", extra={"channel_id": interaction.channel_id})
                view = None
            if view is None:
                await interaction.followup.send(t(self._lang(interaction), "copilot_failed"), ephemeral=True)
            else:
                await interaction.followup.send(view=view, ephemeral=True)
        finally:
            self._copilot_working.discard(key)

    async def _post_staff_reply(self, channel: discord.TextChannel, staff: discord.abc.User, text: str) -> bool:
        """Staff Bulma's ticket_reply (services/staff_tools), posted as a public card signed "-# ✍️ <@staff>" (no
        pings; the text's mentions are defused). It counts as them speaking: the AI's pending answer is dropped and the
        owner's reply countdown starts, as for a typed staff message. History and transcripts read the card as theirs
        (AITicketsCog._signed_turn). False when Discord refused it."""
        try:
            await channel.send(
                view=staff_copilot.posted_card(defuse_mentions(text), staff.id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Could not post a staff reply", extra={"channel_id": channel.id})
            return False
        if (ai := self.bot.get_cog(AI_COG)) is not None:
            ai.staff_replied(channel.id, staff.id)
        try:
            await note_ticket_message(channel.id, staff.id)
        except Exception:
            log.exception("Could not track ticket reply", extra={"channel_id": channel.id})
        return True

    # ---- transcript + archive --------------------------------------------------------------------

    async def closed_spans_for(self, channel_id: int) -> list[Span]:
        """When this channel's ticket was closed; AITicketsCog's text transcript hides the same messages."""
        return closed_spans(await get_ticket_by_channel(channel_id))

    async def export_html(self, channel: discord.TextChannel) -> bytes | None:
        """Standalone .html of the newest messages, or None if it failed or is implausibly large. Messages sent while
        the ticket was closed stay out; a notice marks each closed stretch."""
        try:
            history = [message async for message in channel.history(limit=TRANSCRIPT_MESSAGE_LIMIT)]
            spans = await self.closed_spans_for(channel.id)
            entries = hide_closed_messages(reversed(history), spans, bot_id=getattr(self.bot.user, "id", None))
            # Newest first on purpose: chat_exporter reverses the list itself, so the page reads top-down like Discord.
            messages = [entry for entry in reversed(entries) if not isinstance(entry, ClosedGap)]
            for message in messages:
                # chat_exporter quotes (and fetches, if it must) the message a reply points at; it may be a hidden one.
                reply_to = getattr(message.reference, "message_id", None)
                if reply_to and closed_span_index(discord.utils.snowflake_time(reply_to), spans) is not None:
                    message.reference = None
                inline_forwards(message)  # chat_exporter ignores message.snapshots
            # An empty list would make chat_exporter fetch the whole history itself.
            html = await chat_exporter.raw_export(
                channel,
                messages,
                tz_info="UTC",
                guild=channel.guild,
                bot=self.bot,
                attachment_handler=InlineImageHandler(),
            ) if messages else None
        except Exception:
            log.exception("HTML transcript export failed", extra={"channel_id": channel.id})
            return None
        if html:
            html = polish_transcript(add_closed_notices(html, entries), **self._preview(channel, len(messages)))
        data = (html or "").encode("utf-8")
        if not data or len(data) > PAGE_MAX_BYTES:
            log.warning("HTML transcript empty or too large", extra={"channel_id": channel.id, "bytes": len(data)})
            return None
        return data

    def _preview(self, channel: discord.TextChannel, message_count: int) -> dict:
        topic = channel.topic or ""
        number = re.search(r"#(\d+)", topic)
        category = next((c for c in CATEGORIES.values() if f"· {c.key} ·" in topic), None)
        title = f"Ticket #{number.group(1)}" if number else f"#{channel.name}"
        parts = [f"{category.emoji} {category.name}" if category else None, f"{message_count} messages"]
        return {
            "title": f"{title} · {channel.guild.name}",
            "description": " · ".join(part for part in parts if part),
            "image": channel.guild.icon.url if channel.guild.icon else None,
        }

    async def build_page(self, channel: discord.TextChannel) -> StoredPage | None:
        """Export the channel and host it on disk, over the channel's existing page if it has one (same address);
        AITicketsCog records the token with the rest of the close-out."""
        html = await self.export_html(channel)
        if html is None:
            return None
        return await save_page(self.settings, html, reuse=await get_page_for_channel(channel.id))

    async def archive(
        self,
        channel: discord.TextChannel,
        ticket: Ticket,
        *,
        closed_by_id: int | None,
        card: discord.Message | None = None,
        resolved: bool | None = None,
        dm: bool = True,
    ) -> StoredPage | None:
        """The one archive entry point (every close, and the closed card's Retry/Transcript button): transcript +
        summary into the DB and the hosted page, the closed card's transcript button becomes the link (or Retry),
        and the owner gets the link by DM (unless dm=False). Returns the page, None if archiving failed."""
        page = None
        if await self._archive(channel, ticket, closed_by_id, resolved):
            try:
                page = await get_page_for_channel(channel.id)
            except Exception:
                log.exception("Could not look up hosted transcript", extra={"channel_id": channel.id})
        if card is not None:
            url = page_url(self.settings, page.token) if page else None
            await self._set_row(card, closed_row(url, auto_close=ticket.delete_at is not None))
        if dm and page is not None and self.settings.ticket_dm_transcript:
            await self._dm_transcript_link(channel, ticket, page)
        return page

    async def _dm_transcript_link(self, channel: discord.TextChannel, ticket: Ticket, page: StoredPage) -> None:
        owner = channel.guild.get_member(ticket.owner_id)
        if owner is None:
            return
        try:
            url = page_url(self.settings, page.token)
            await owner.send(view=dm_transcript(ticket.ticket_id, channel.guild, url, page.expires_at, ticket.language))
        except discord.HTTPException:
            log.info("Could not DM ticket transcript", extra={"user_id": ticket.owner_id})

    async def delete_ticket(self, channel: discord.TextChannel, ticket: Ticket, *, on_notice=None) -> bool:
        """Every deletion of a closed ticket (the close already archived the transcript): a notice in the channel,
        DELETE_NOTICE_SECONDS, then the delete. The deadline goes in delete_at first, so after a restart the sweep
        finishes it; a staff re-open during the notice wins. on_notice: awaited once the notice is up.
        False only when Discord refused the delete; already being deleted, or re-opened meanwhile, is not a failure."""
        if channel.id in self._deleting:
            return True
        self._deleting.add(channel.id)
        try:
            delete_at = datetime.now(timezone.utc) + timedelta(seconds=DELETE_NOTICE_SECONDS)
            scheduled = await schedule_delete(channel.id, delete_at)
            if scheduled is None:
                return True  # re-opened (or already gone) meanwhile
            await self._safely(
                channel.send(
                    view=deleting_notice(delete_at, scheduled.language), allowed_mentions=discord.AllowedMentions.none()
                ),
                channel,
            )
            if on_notice is not None:
                await on_notice()
            await asyncio.sleep(DELETE_NOTICE_SECONDS)
            current = await get_ticket_by_channel(channel.id)
            if current is None or current.status != STATUS_CLOSED or current.delete_at is None:
                return True  # staff re-opened it during the notice
            await mark_deleted(channel.id)
            await channel.delete(reason=f"Ticket #{ticket.ticket_id:04d} deleted")
            return True
        except discord.NotFound:
            return True  # the channel was already gone
        except discord.HTTPException:
            log.exception("Failed to delete ticket", extra={"channel_id": channel.id})
            return False
        finally:
            self._deleting.discard(channel.id)

    async def _archive(
        self,
        channel: discord.TextChannel,
        ticket: Ticket,
        closed_by_id: int | None,
        resolved: bool | None = None,
    ) -> bool:
        ai_cog = self.bot.get_cog(AI_COG)
        if ai_cog is not None:
            # Every call here is a deliberate (re-)archive, so lift the AI cog's once-per-close dedupe first.
            ai_cog.forget_archived(channel.id)
            # Records the transcript (with the hosted page) for the panel.
            try:
                return await ai_cog._close_ticket(
                    channel,
                    closed_by_id=closed_by_id,
                    requester_id=ticket.owner_id,
                    resolved=resolved,
                )
            except Exception:
                # e.g. the closed-stretch lookup failed; no transcript beats one that shows hidden messages.
                log.exception("Ticket archive failed", extra={"channel_id": channel.id})
                return False
        # Without the AI cog there is no DB row or hosted page, so there is nothing to show in the panel.
        log.warning("AI tickets cog isn't loaded; ticket transcript not archived", extra={"channel_id": channel.id})
        return False

    # ---- /ticket ---------------------------------------------------------------------------------

    ticket = discord.SlashCommandGroup("ticket", **described(DESC, "ticket"))

    async def _command_gate(
        self, ctx: discord.ApplicationContext, minimum: Rank, *, denied: str = "staff_only"
    ) -> Ticket | None:
        ticket = await get_ticket_by_channel(ctx.channel_id) if ctx.channel_id else None
        if ticket is None:
            await ctx.respond(t(self._lang(ctx), "not_ticket"), ephemeral=True)
            return None
        if member_rank(ctx.author, ticket.owner_id, self.settings) < minimum:
            await ctx.respond(t(self._lang(ctx), denied), ephemeral=True)
            return None
        return ticket

    @ticket.command(name="panel", **described(DESC, "panel"))
    @discord.option("channel", discord.TextChannel, **described(DESC, "panel.channel"), required=False)
    async def panel(self, ctx: discord.ApplicationContext, channel: discord.TextChannel | None = None):
        if not (is_admin(ctx.author) or member_rank(ctx.author, None, self.settings) >= Rank.MOD):
            return await ctx.respond(t(self._lang(ctx), "mod_only"), ephemeral=True)
        target = channel or ctx.channel
        await ctx.defer(ephemeral=True)
        await target.send(view=panel_view())
        await ctx.interaction.delete_original_response()  # the panel appearing is the confirmation

    async def add_to_ticket(self, channel: discord.TextChannel, target: discord.Member | discord.Role, reason: str) -> None:
        overwrite = channel.overwrites_for(target)
        overwrite.view_channel = True
        await channel.set_permissions(target, overwrite=overwrite, reason=reason)

    @ticket.command(name="add", **described(DESC, "add"))
    @discord.option("target", discord.abc.Mentionable, **described(DESC, "add.target"))
    async def add(self, ctx: discord.ApplicationContext, target: discord.Member | discord.Role):
        if await self._command_gate(ctx, Rank.HELPER) is None:
            return
        await self.add_to_ticket(ctx.channel, target, f"/ticket add by {ctx.author}")
        await ctx.respond(f"➕ {target.mention} was added to this ticket.", allowed_mentions=discord.AllowedMentions.none())

    @ticket.command(name="assign", **described(DESC, "assign"))
    @discord.option("member", discord.Member, **described(DESC, "assign.member"))
    async def assign(self, ctx: discord.ApplicationContext, member: discord.Member):
        ticket = await self._command_gate(ctx, Rank.HELPER)
        if ticket is None:
            return
        if member.bot:
            return await ctx.respond(t(self._lang(ctx), "assign_bot"), ephemeral=True)
        # ponytail: no stored assignee, the ping is the record; add a column if a "my tickets" view is wanted.
        overwrite = ctx.channel.overwrites_for(member)
        overwrite.view_channel = True
        overwrite.send_messages = True
        overwrite.read_message_history = True
        await ctx.channel.set_permissions(member, overwrite=overwrite, reason=f"/ticket assign by {ctx.author}")
        # Addressed to the assignee, so in their language.
        language = user_language(member, self.settings, client=self.bot)
        await ctx.respond(
            t(language, "assigned", mention=member.mention, by=ctx.author.mention),
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )
        try:
            await member.send(view=dm_assigned(ticket.ticket_id, ctx.channel, ctx.author, language))
        except discord.HTTPException:
            pass  # DMs closed; the ping in the ticket still reaches them

    @ticket.command(name="remove", **described(DESC, "remove"))
    @discord.option("target", discord.abc.Mentionable, **described(DESC, "remove.target"))
    async def remove(self, ctx: discord.ApplicationContext, target: discord.Member | discord.Role):
        ticket = await self._command_gate(ctx, Rank.HELPER)
        if ticket is None:
            return
        protected = (
            target.id == ticket.owner_id
            if isinstance(target, discord.Role)
            else member_rank(target, ticket.owner_id, self.settings) >= Rank.OWNER
        )
        if protected or target == ctx.guild.default_role:
            return await ctx.respond(t(self._lang(ctx), "protected_target"), ephemeral=True)
        overwrite = ctx.channel.overwrites_for(target)
        overwrite.view_channel = False
        await ctx.channel.set_permissions(target, overwrite=overwrite, reason=f"/ticket remove by {ctx.author}")
        await ctx.respond(f"➖ {target.mention} was removed from this ticket.", allowed_mentions=discord.AllowedMentions.none())

    @ticket.command(name="close", **described(DESC, "close"))
    @discord.option("reason", str, **described(DESC, "close.reason"), required=False, max_length=200)
    async def close(self, ctx: discord.ApplicationContext, reason: str | None = None):
        ticket = await self._command_gate(ctx, Rank.OWNER, denied="no_permission")
        if ticket is None:
            return
        await ctx.defer(ephemeral=True)
        closed = await self.close_ticket(ctx.channel, closer_id=ctx.author.id, reason=reason)
        page = await get_page_for_channel(ctx.channel.id) if closed else None
        if page is not None:
            language = self._reply_language(ctx, closed)
            link = transcript_link_view(closed.ticket_id, page_url(self.settings, page.token), language)
            await ctx.interaction.edit_original_response(view=link)
        else:
            await ctx.respond(t(self._lang(ctx), "transcript_failed" if closed else "already_closed"), ephemeral=True)

    @ticket.command(name="transcript", **described(DESC, "transcript"))
    async def transcript(self, ctx: discord.ApplicationContext):
        ticket = await self._command_gate(ctx, Rank.HELPER)
        if ticket is None:
            return
        await ctx.defer()
        html = await self.export_html(ctx.channel)
        if html is None or len(html) > ctx.guild.filesize_limit - 65_536:
            await ctx.interaction.delete_original_response()  # keep the failure private
            return await ctx.followup.send(t(self._lang(ctx), "transcript_failed"), ephemeral=True)
        await ctx.respond(file=discord.File(io.BytesIO(html), filename=f"ticket-{ticket.ticket_id:04d}.html"))


def setup(bot: discord.Bot):
    bot.add_cog(TicketsCog(bot))
