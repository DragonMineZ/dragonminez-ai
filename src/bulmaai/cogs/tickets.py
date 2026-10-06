"""In-house ticket system: dropdown panel → intake modal → private channel → close (moves to the closed category).
Transcripts (hosted HTML) are only made when staff press Transcript, or when the AI solved the ticket on its own.

AI answers inside tickets still come from AITicketsCog (same category); this cog owns the lifecycle.
"""

import asyncio
import base64
import html as html_lib
import io
import logging
import re
from enum import IntEnum

import chat_exporter
import discord
from chat_exporter import AttachmentHandler
from discord.ext import commands, tasks

from bulmaai.services import ai_budget, mod_actions
from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.services.ticket_intake import TicketIntake, triage_intake
from bulmaai.services.ticket_pages import StoredPage, get_page_for_channel, page_url, purge, save_page
from bulmaai.services.tickets import (
    STATUS_OPEN,
    Ticket,
    abandon_ticket,
    attach_channel,
    closed_channel_name,
    get_open_tickets_by_owner,
    get_ticket_by_channel,
    list_active_tickets,
    mark_closed,
    mark_deleted,
    mark_reopened,
    open_channel_name,
    reserve_ticket,
)
from bulmaai.ui.support_views import PATREON_URL
from bulmaai.ui.ticket_views import (
    CATEGORIES,
    MSG,
    TicketCategory,
    TRANSCRIPT_BUTTON_ID,
    TicketClosedView,
    TicketControlView,
    TicketPanelView,
    build_panel_embed,
    build_ticket_embed,
    closed_embed,
    dm_created,
    dm_transcript,
    msg_created,
    msg_limit,
    msg_transcript_ready,
    reopened_embed,
)
from bulmaai.utils.language import detect_language_from_text
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
PING_WARN_REASON = "Pinged staff in a ticket"
# chat_exporter's <head> puts raw HTML (emoji <img>) into the link-preview meta tags; ours replaces it.
_HEAD_META = re.compile(r"<title>.*?(?=<style>)", re.S)
_DONATE = re.compile(r'<a href="https://ko-fi\.com/mahtoid">DONATE</a>')


class Rank(IntEnum):
    NONE = 0
    TESTER = 1
    OWNER = 2
    HELPER = 3
    MOD = 4


def _ids(*groups) -> set[int]:
    return {int(role_id) for group in groups for role_id in group}


def member_rank(member: discord.abc.User, owner_id: int | None, settings) -> Rank:
    """Highest hat wins. A tester who isn't staff or the ticket's owner is read-only, whatever else they click."""
    if is_bruno(member):
        return Rank.MOD
    role_ids = {role.id for role in getattr(member, "roles", [])}
    if role_ids & _ids(settings.panel_owner_role_ids, settings.panel_admin_role_ids, settings.panel_moderator_role_ids):
        return Rank.MOD
    if role_ids & _ids(settings.panel_helper_role_ids):
        return Rank.HELPER
    if member.id == owner_id:
        return Rank.OWNER
    if role_ids & _ids(settings.ticket_tester_role_ids):
        return Rank.TESTER
    return Rank.NONE


def build_overwrites(
    guild: discord.Guild, owner: discord.Member, settings
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    mods = _ids(settings.panel_owner_role_ids, settings.panel_admin_role_ids, settings.panel_moderator_role_ids)
    helpers = _ids(settings.panel_helper_role_ids) - mods
    testers = _ids(settings.ticket_tester_role_ids) - mods - helpers

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
        owner: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
    }
    staff_kwargs = dict(view_channel=True, send_messages=True, read_message_history=True)
    for role_ids, overwrite in (
        (testers, discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=False, add_reactions=False
        )),
        (helpers, discord.PermissionOverwrite(manage_messages=False, **staff_kwargs)),
        (mods, discord.PermissionOverwrite(manage_messages=True, **staff_kwargs)),
    ):
        for role_id in role_ids:
            if (role := guild.get_role(role_id)) is not None:
                overwrites[role] = overwrite
    return overwrites


def staff_role_ids(settings) -> set[int]:
    return _ids(
        settings.panel_owner_role_ids,
        settings.panel_admin_role_ids,
        settings.panel_moderator_role_ids,
        settings.panel_helper_role_ids,
    )


def pings_staff(message: discord.Message, settings) -> bool:
    """Typed <@user>/<@&role> mentions of staff only; reply pings aren't in the content, so they don't count."""
    if set(message.raw_role_mentions) & staff_role_ids(settings):
        return True
    guild = message.guild
    return any(
        (target := guild.get_member(user_id)) is not None and member_rank(target, None, settings) >= Rank.HELPER
        for user_id in message.raw_mentions
    )


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

    @property
    def settings(self):
        return self.bot.settings

    async def on_startup(self) -> None:
        for view in (TicketPanelView(), TicketControlView(), TicketClosedView()):
            self.bot.add_view(view)
        if not self._purge_pages.is_running():
            self._purge_pages.start()
        try:
            # Channels deleted while the bot was offline would otherwise count against their owner's limit.
            for ticket in await list_active_tickets():
                if self.bot.get_channel(ticket.channel_id) is None:
                    await mark_deleted(ticket.channel_id)
        except Exception:
            log.exception("Failed to reconcile ticket channels")

    async def on_shutdown(self) -> None:
        self._purge_pages.cancel()

    @tasks.loop(hours=1)
    async def _purge_pages(self) -> None:
        try:
            removed = await purge(self.settings)
        except Exception:
            log.exception("Transcript purge failed")
            return
        if removed:
            log.info("Purged %d expired or orphaned ticket transcript files", removed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        try:
            await mark_deleted(channel.id)
        except Exception:
            log.exception("Failed to mark ticket channel deleted", extra={"channel_id": channel.id})

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        settings = self.settings
        if (
            message.author.bot
            or not isinstance(message.author, discord.Member)
            or not (message.raw_mentions or message.raw_role_mentions)
            or getattr(message.channel, "category_id", None) != settings.ai_ticket_category_id
            or member_rank(message.author, None, settings) >= Rank.HELPER
            or not pings_staff(message, settings)
        ):
            return
        try:
            if await get_ticket_by_channel(message.channel.id) is None:
                return
            await mod_actions.perform(
                self.bot,
                message.guild,
                action="warn",
                target_id=message.author.id,
                moderator=None,
                reason=PING_WARN_REASON,
                source="automod",
            )
        except Exception:
            log.exception("Staff-ping warn failed", extra={"user_id": message.author.id})
            return
        await self._safely(
            message.reply(
                f"{message.author.mention}, {MSG['no_staff_pings']}",
                allowed_mentions=discord.AllowedMentions(users=[message.author], replied_user=False),
            ),
            message.channel,
        )

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

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        category: TicketCategory,
        answers: list[tuple[str, str]],
    ) -> None:
        guild, member, settings = interaction.guild, interaction.user, self.settings
        # invisible=False: a modal submit's default ("invisible") defer would edit the public panel message instead.
        await interaction.response.defer(ephemeral=True, invisible=False)
        if guild is None or not isinstance(member, discord.Member):
            return
        await interaction.edit_original_response(content=MSG["creating"])

        form_text = "\n".join(f"{label}: {value}" for label, value in answers)
        ticket = await reserve_ticket(
            guild_id=guild.id,
            owner_id=member.id,
            category=category.key,
            language=detect_language_from_text(form_text),
            max_open=settings.ticket_max_open_per_user,
        )
        if ticket is None:
            return await interaction.edit_original_response(content=msg_limit(settings.ticket_max_open_per_user))

        channel: discord.TextChannel | None = None
        try:
            intake = await self._triage(category, answers, ticket)
            parent = guild.get_channel(settings.ai_ticket_category_id) if settings.ai_ticket_category_id else None
            if not isinstance(parent, discord.CategoryChannel):
                raise RuntimeError("ai_ticket_category_id is not a category channel")
            name = open_channel_name(intake.channel_slug, ticket.ticket_id)
            channel = await guild.create_text_channel(
                name,
                category=parent,
                overwrites=build_overwrites(guild, member, settings),
                topic=f"Ticket #{ticket.ticket_id:04d} · {category.key} · owner {member.id}",
                reason=f"Ticket #{ticket.ticket_id:04d} opened",
            )
            welcome = await channel.send(
                content=member.mention,
                embed=build_ticket_embed(number=ticket.ticket_id, category=category, answers=answers),
                view=TicketControlView(),
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
            await attach_channel(
                ticket.ticket_id,
                channel_id=channel.id,
                channel_name=name,
                language=intake.language,
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
            return await interaction.edit_original_response(content=MSG["failed"])

        await interaction.edit_original_response(content=msg_created(channel.mention))
        try:
            embed, view = dm_created(ticket.ticket_id, category, channel)
            await member.send(embed=embed, view=view)
        except discord.HTTPException:
            pass  # DMs closed; the ephemeral message above already has the link

        ai_cog = self.bot.get_cog(AI_COG)
        if ai_cog is not None and category.ai_reply:
            await ai_cog.answer_new_ticket(
                channel, member, category_label=category.name, form_text=form_text, language=intake.language
            )

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
                model=settings.openai_ticket_summary_model,
                fallback_slug=category.fallback_slug,
            )
        except Exception:
            log.exception("Ticket intake triage failed", extra={"ticket_id": ticket.ticket_id})
            return fallback

    # ---- close / reopen ---------------------------------------------------------------------------

    async def close_ticket(
        self, channel: discord.TextChannel, *, closer_id: int | None, reason: str | None
    ) -> Ticket | None:
        """Lock the owner out, move to the closed category, rename, post the closed embed. None if it wasn't open."""
        ticket = await mark_closed(channel.id, closed_by=closer_id, reason=reason)
        if ticket is None:
            return None
        owner = channel.guild.get_member(ticket.owner_id)
        if owner is not None:
            overwrite = channel.overwrites_for(owner)
            overwrite.send_messages = False
            await self._safely(channel.set_permissions(owner, overwrite=overwrite, reason="Ticket closed"), channel)
        await self._move(channel, self.settings.ticket_closed_category_id)
        await self._rename(channel, closed_channel_name(ticket.ticket_id))
        await self._set_buttons(channel, ticket, None)
        await self._safely(
            channel.send(
                embed=closed_embed(f"<@{closer_id}>" if closer_id else None, reason and defuse_mentions(reason)),
                view=TicketClosedView(),
                allowed_mentions=discord.AllowedMentions.none(),
            ),
            channel,
        )
        return ticket

    async def reopen_ticket(self, channel: discord.TextChannel, *, opener_id: int) -> Ticket | None:
        ticket = await mark_reopened(channel.id)
        if ticket is None:
            return None
        if (ai_cog := self.bot.get_cog(AI_COG)) is not None:
            ai_cog.forget_archived(channel.id)
        owner = channel.guild.get_member(ticket.owner_id)
        if owner is not None:
            overwrite = channel.overwrites_for(owner)
            overwrite.send_messages = True
            await self._safely(channel.set_permissions(owner, overwrite=overwrite, reason="Ticket re-opened"), channel)
        await self._move(channel, self.settings.ai_ticket_category_id)
        await self._rename(channel, ticket.channel_name or open_channel_name("ticket", ticket.ticket_id))
        await self._set_buttons(channel, ticket, TicketControlView())
        await self._safely(
            channel.send(embed=reopened_embed(f"<@{opener_id}>"), allowed_mentions=discord.AllowedMentions.none()),
            channel,
        )
        return ticket

    async def _safely(self, coro, channel: discord.TextChannel) -> None:
        try:
            await coro
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

    async def _set_buttons(self, channel: discord.TextChannel, ticket: Ticket, view: discord.ui.View | None) -> None:
        if ticket.control_message_id is None:
            return
        await self._safely(channel.get_partial_message(ticket.control_message_id).edit(view=view), channel)

    # ---- button handlers (called from ui/ticket_views.py) ----------------------------------------

    async def _gate(
        self, interaction: discord.Interaction, minimum: Rank, *, denied: str = "no_permission", want_open: bool | None = True
    ) -> Ticket | None:
        """Ticket for this channel if the clicker may act on it; otherwise answers ephemerally and returns None."""
        ticket = await get_ticket_by_channel(interaction.channel_id) if interaction.channel_id else None
        if ticket is None:
            await interaction.response.send_message(MSG["not_ticket"], ephemeral=True)
            return None
        if member_rank(interaction.user, ticket.owner_id, self.settings) < minimum:
            await interaction.response.send_message(MSG[denied], ephemeral=True)
            return None
        if want_open is not None and (ticket.status == STATUS_OPEN) != want_open:
            await interaction.response.send_message(MSG["already_closed" if want_open else "not_closed"], ephemeral=True)
            return None
        return ticket

    async def on_close(self, interaction: discord.Interaction) -> None:
        ticket = await self._gate(interaction, Rank.OWNER)
        if ticket is None:
            return
        await interaction.response.defer()
        await self.close_ticket(interaction.channel, closer_id=interaction.user.id, reason=None)

    async def on_transcript(self, interaction: discord.Interaction) -> None:
        ticket = await self._gate(interaction, Rank.HELPER, denied="staff_only", want_open=None)
        if ticket is None:
            return
        await interaction.response.defer()
        await self.archive(interaction.channel, ticket, closed_by_id=ticket.closed_by or interaction.user.id)
        page = await get_page_for_channel(interaction.channel.id)
        if page is None:
            return await interaction.followup.send(MSG["transcript_failed"], ephemeral=True)
        view = TicketClosedView()
        view.get_item(TRANSCRIPT_BUTTON_ID).disabled = True
        await self._safely(interaction.message.edit(view=view), interaction.channel)
        await interaction.followup.send(msg_transcript_ready(page_url(self.settings, page.token)))

    async def on_reopen(self, interaction: discord.Interaction) -> None:
        ticket = await self._gate(interaction, Rank.MOD, denied="mod_only", want_open=False)
        if ticket is None:
            return
        if interaction.guild.get_member(ticket.owner_id) is None:
            return await interaction.response.send_message(MSG["owner_left"], ephemeral=True)
        await interaction.response.edit_message(view=None)  # this closed embed's buttons are spent
        await self.reopen_ticket(interaction.channel, opener_id=interaction.user.id)

    async def on_delete(self, interaction: discord.Interaction) -> None:
        ticket = await self._gate(interaction, Rank.MOD, denied="mod_only", want_open=False)
        if ticket is None:
            return
        await interaction.response.send_message(MSG["deleting"])
        if not await self.delete_ticket(interaction.channel, ticket):
            await interaction.followup.send(MSG["delete_failed"], ephemeral=True)

    # ---- transcript + archive --------------------------------------------------------------------

    async def export_html(self, channel: discord.TextChannel) -> bytes | None:
        """Standalone .html of the newest messages, or None if it failed or is implausibly large."""
        try:
            # Newest first on purpose: chat_exporter reverses the list itself, so the page reads top-down like Discord.
            messages = [message async for message in channel.history(limit=TRANSCRIPT_MESSAGE_LIMIT)]
            html = await chat_exporter.raw_export(
                channel,
                messages,
                tz_info="UTC",
                guild=channel.guild,
                bot=self.bot,
                attachment_handler=InlineImageHandler(),
            )
        except Exception:
            log.exception("HTML transcript export failed", extra={"channel_id": channel.id})
            return None
        if html:
            html = polish_transcript(html, **self._preview(channel, len(messages)))
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
        """Export the channel and host it on disk; AITicketsCog records the token with the rest of the close-out."""
        html = await self.export_html(channel)
        return await save_page(self.settings, html) if html is not None else None

    async def archive(self, channel: discord.TextChannel, ticket: Ticket, *, closed_by_id: int | None) -> bool:
        """Transcript + summary into the archive channel and the DB, then the owner gets the hosted link by DM."""
        archived = await self._archive(channel, ticket, closed_by_id)
        if archived and self.settings.ticket_dm_transcript:
            await self._dm_transcript_link(channel, ticket)
        return archived

    async def _dm_transcript_link(self, channel: discord.TextChannel, ticket: Ticket) -> None:
        owner = channel.guild.get_member(ticket.owner_id)
        try:
            page = await get_page_for_channel(channel.id)
        except Exception:
            log.exception("Could not look up hosted transcript", extra={"channel_id": channel.id})
            return
        if owner is None or page is None:
            return
        try:
            embed, view = dm_transcript(ticket.ticket_id, channel.guild, page_url(self.settings, page.token), page.expires_at)
            await owner.send(embed=embed, view=view)
        except discord.HTTPException:
            log.info("Could not DM ticket transcript", extra={"user_id": ticket.owner_id})

    async def delete_ticket(self, channel: discord.TextChannel, ticket: Ticket) -> bool:
        """Just deletes; a transcript exists only if staff asked for one (or the AI solved it, see AITicketsCog)."""
        if channel.id in self._deleting:
            return False
        self._deleting.add(channel.id)
        try:
            await mark_deleted(channel.id)
            await channel.delete(reason=f"Ticket #{ticket.ticket_id:04d} deleted")
            return True
        except discord.HTTPException:
            log.exception("Failed to delete ticket", extra={"channel_id": channel.id})
            return False
        finally:
            self._deleting.discard(channel.id)

    async def _archive(self, channel: discord.TextChannel, ticket: Ticket, closed_by_id: int | None) -> bool:
        ai_cog = self.bot.get_cog(AI_COG)
        if ai_cog is not None:
            # Posts the summary embed to the archive channel and records it (with the hosted page) for the panel.
            return await ai_cog._close_ticket(
                channel,
                closed_by_id=closed_by_id,
                requester_id=ticket.owner_id,
                resolved=None,
                delete_channel=False,
                announce=False,
            )
        # ponytail: without the AI cog there is no DB row or hosted page, so the file goes straight to the archive channel.
        html = await self.export_html(channel)
        target_id = self.settings.ai_ticket_transcript_channel_id
        if html is None or target_id is None or len(html) > channel.guild.filesize_limit - 65_536:
            return False
        try:
            target = self.bot.get_channel(target_id) or await self.bot.fetch_channel(target_id)
            await target.send(
                f"🎫 Ticket #{ticket.ticket_id:04d} ({ticket.category}) · owner <@{ticket.owner_id}>",
                file=discord.File(io.BytesIO(html), filename=f"{channel.name}-transcript.html"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.HTTPException:
            log.exception("Failed to post ticket transcript", extra={"transcript_channel_id": target_id})
            return False

    # ---- /ticket ---------------------------------------------------------------------------------

    ticket = discord.SlashCommandGroup("ticket", "Ticket tools")

    async def _command_gate(
        self, ctx: discord.ApplicationContext, minimum: Rank, *, denied: str = "staff_only"
    ) -> Ticket | None:
        ticket = await get_ticket_by_channel(ctx.channel_id) if ctx.channel_id else None
        if ticket is None:
            await ctx.respond(MSG["not_ticket"], ephemeral=True)
            return None
        if member_rank(ctx.author, ticket.owner_id, self.settings) < minimum:
            await ctx.respond(MSG[denied], ephemeral=True)
            return None
        return ticket

    @ticket.command(name="panel", description="Post the ticket panel in a channel")
    @discord.option("channel", discord.TextChannel, description="Where to post it (default: here)", required=False)
    async def panel(self, ctx: discord.ApplicationContext, channel: discord.TextChannel | None = None):
        if not (is_admin(ctx.author) or member_rank(ctx.author, None, self.settings) >= Rank.MOD):
            return await ctx.respond(MSG["mod_only"], ephemeral=True)
        target = channel or ctx.channel
        await target.send(embed=build_panel_embed(), view=TicketPanelView())
        await ctx.respond(MSG["done"], ephemeral=True)

    @ticket.command(name="add", description="Let a user or role see this ticket")
    @discord.option("target", discord.abc.Mentionable, description="User or role to add")
    async def add(self, ctx: discord.ApplicationContext, target: discord.Member | discord.Role):
        if await self._command_gate(ctx, Rank.HELPER) is None:
            return
        overwrite = ctx.channel.overwrites_for(target)
        overwrite.view_channel = True
        await ctx.channel.set_permissions(target, overwrite=overwrite, reason=f"/ticket add by {ctx.author}")
        await ctx.respond(MSG["done"], ephemeral=True)

    @ticket.command(name="remove", description="Hide this ticket from a user or role")
    @discord.option("target", discord.abc.Mentionable, description="User or role to remove")
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
            return await ctx.respond(MSG["protected_target"], ephemeral=True)
        overwrite = ctx.channel.overwrites_for(target)
        overwrite.view_channel = False
        await ctx.channel.set_permissions(target, overwrite=overwrite, reason=f"/ticket remove by {ctx.author}")
        await ctx.respond(MSG["done"], ephemeral=True)

    @ticket.command(name="close", description="Close this ticket")
    @discord.option("reason", str, description="Why it's being closed", required=False, max_length=200)
    async def close(self, ctx: discord.ApplicationContext, reason: str | None = None):
        ticket = await self._command_gate(ctx, Rank.OWNER, denied="no_permission")
        if ticket is None:
            return
        await ctx.defer(ephemeral=True)
        closed = await self.close_ticket(ctx.channel, closer_id=ctx.author.id, reason=reason)
        await ctx.respond(MSG["done"] if closed else MSG["already_closed"], ephemeral=True)

    @ticket.command(name="transcript", description="Export this ticket's transcript as HTML")
    async def transcript(self, ctx: discord.ApplicationContext):
        ticket = await self._command_gate(ctx, Rank.HELPER)
        if ticket is None:
            return
        await ctx.defer(ephemeral=True)
        html = await self.export_html(ctx.channel)
        if html is None or len(html) > ctx.guild.filesize_limit - 65_536:
            return await ctx.respond(MSG["transcript_failed"], ephemeral=True)
        await ctx.respond(
            file=discord.File(io.BytesIO(html), filename=f"ticket-{ticket.ticket_id:04d}.html"), ephemeral=True
        )


def setup(bot: discord.Bot):
    bot.add_cog(TicketsCog(bot))
