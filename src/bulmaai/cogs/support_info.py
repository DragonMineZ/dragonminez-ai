"""Staff support info: /info to manage entries, the daily public post of active temporary ones, the staff review
card for suggestions the ticket close summary makes (suggest_from_ticket / finish_suggestion), and the weekly
"Answers to fix" cards (ui/feedback_fix, posted by cogs/support_report.py): staff write the right answer to a 👎 answer,
which goes live as support info (source feedback), or skip it.

The post is DB-driven (support_info_post.posted_at) and checked every POLL_MINUTES, so restarts don't skip or repeat
it; claim_daily_post makes sure only one loop posts."""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from bulmaai.services import ai_feedback as feedback_store
from bulmaai.services import support_info as db
from bulmaai.ui import feedback_fix
from bulmaai.ui.support_info_views import (
    APPROVE,
    EDIT,
    daily_post_card,
    info_list_card,
    parse_review_custom_id,
    review_card,
    review_handled_card,
)
from bulmaai.ui.v2 import unbusy, while_busy
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import in_main_guild, is_staff

log = logging.getLogger(__name__)

POLL_MINUTES = 15
STAFF_ONLY_MSG = "Only staff can manage support info."
NOT_FOUND_MSG = "No active support info #{id}."
ALREADY_HANDLED_MSG = "Already handled."
REVIEW_FAILED_MSG = "Couldn't update that suggestion. Try again."
DAYS_MSG = "Expires in days must be a whole number from 1 to 365."
FIX_HANDLED_MSG = "Someone already handled this one."
FIX_FAILED_MSG = "Couldn't save that. Try again."
# An approved temporary suggestion without its own expiry stops feeding the AI after this long.
SUGGESTION_EXPIRY = timedelta(days=7)


def _is_staff_member(user, settings) -> bool:
    return isinstance(user, discord.Member) and in_main_guild(user, settings) and is_staff(user, settings=settings)


class InfoModal(discord.ui.DesignerModal):
    """Title + text. info_id None: a new entry of kind (expires_at for temporary); else edits that entry."""

    def __init__(
        self,
        *,
        kind: str,
        expires_at: datetime | None = None,
        info_id: int | None = None,
        title: str = "",
        body: str = "",
    ) -> None:
        self.title_input = discord.ui.InputText(
            style=discord.InputTextStyle.short, max_length=db.TITLE_MAX, value=title or None
        )
        self.body_input = discord.ui.InputText(
            style=discord.InputTextStyle.paragraph, max_length=db.BODY_MAX, value=body or None
        )
        heading = f"Edit support info #{info_id}" if info_id else f"New {kind} support info"
        super().__init__(
            discord.ui.Label("Title", self.title_input),
            discord.ui.Label("Text", self.body_input),
            title=heading,
        )
        self.kind, self.expires_at, self.info_id = kind, expires_at, info_id

    async def callback(self, interaction: discord.Interaction) -> None:
        title, body = self.title_input.value or "", self.body_input.value or ""
        try:
            if self.info_id is None:
                entry = await db.add_info(
                    kind=self.kind, title=title, body=body, created_by=interaction.user.id, expires_at=self.expires_at
                )
            else:
                entry = await db.update_info(
                    self.info_id,
                    kind=self.kind,
                    title=title,
                    body=body,
                    expires_at=self.expires_at,
                    updated_by=interaction.user.id,
                )
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)
        if entry is None:
            return await interaction.response.send_message(NOT_FOUND_MSG.format(id=self.info_id), ephemeral=True)
        await interaction.response.send_message(f"Saved #{entry.id}.", ephemeral=True)


class SuggestionModal(discord.ui.DesignerModal):
    """Edit & approve on a review card: title, text, kind and an optional expiry in days, then approve."""

    def __init__(self, cog: "SupportInfoCog", entry: db.SupportInfo) -> None:
        self.title_input = discord.ui.InputText(
            style=discord.InputTextStyle.short, max_length=db.TITLE_MAX, value=entry.title
        )
        self.body_input = discord.ui.InputText(
            style=discord.InputTextStyle.paragraph, max_length=db.BODY_MAX, value=entry.body
        )
        self.kind_select = discord.ui.Select(
            options=[
                discord.SelectOption(label=kind.capitalize(), value=kind, default=kind == entry.kind)
                for kind in db.KINDS
            ],
            required=True,
        )
        self.days_input = discord.ui.InputText(style=discord.InputTextStyle.short, max_length=3, required=False)
        super().__init__(
            discord.ui.Label("Title", self.title_input),
            discord.ui.Label("Text", self.body_input),
            discord.ui.Label("Kind", self.kind_select),
            discord.ui.Label(
                "Expires in days",
                self.days_input,
                description=f"Temporary only. Empty: {SUGGESTION_EXPIRY.days} days after approval.",
            ),
            title=f"Edit & approve #{entry.id}",
        )
        self.cog, self.info_id, self.kind = cog, entry.id, entry.kind

    async def callback(self, interaction: discord.Interaction) -> None:
        kind = (self.kind_select.values or [self.kind])[0]
        days = (self.days_input.value or "").strip()
        if days and not (days.isdigit() and 1 <= int(days) <= 365):
            return await interaction.response.send_message(DAYS_MSG, ephemeral=True)
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(days)) if days else None
        try:
            updated = await db.update_info(
                self.info_id,
                kind=kind,
                title=self.title_input.value or "",
                body=self.body_input.value or "",
                expires_at=expires_at,
                updated_by=interaction.user.id,
                only_from=("suggested",),
            )
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)
        if updated is None:
            return await interaction.response.send_message(ALREADY_HANDLED_MSG, ephemeral=True)
        await interaction.response.defer(ephemeral=True, invisible=False)
        entry = await self.cog.finish_suggestion(self.info_id, status="active", by_id=interaction.user.id)
        done = f"Approved as support info #{entry.id}." if entry else ALREADY_HANDLED_MSG
        await interaction.followup.send(done, ephemeral=True)


class SupportInfoCog(ReloadableCog):
    info = discord.SlashCommandGroup("info", "Support info the AI uses and the daily known-issues post")

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._working: set[int] = set()  # review cards with a click in flight (ui/v2.while_busy)

    async def on_startup(self) -> None:
        if not self.daily_post.is_running():
            self.daily_post.start()

    async def on_shutdown(self) -> None:
        self.daily_post.cancel()

    # ---- daily public post -----------------------------------------------------------------------------------------

    @tasks.loop(minutes=POLL_MINUTES)
    async def daily_post(self) -> None:
        try:
            await self._post_tick()
        except Exception:
            log.exception("Support info daily post failed")

    @daily_post.before_loop
    async def _before_daily_post(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_tick(self) -> None:
        previous = await db.claim_daily_post()
        if previous is None:
            return
        if previous.channel_id and previous.message_id:
            old = self.bot.get_partial_messageable(previous.channel_id).get_partial_message(previous.message_id)
            try:
                await old.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        channel_id = self.bot.settings.support_info_channel_id
        entries = [entry for entry in await db.list_active() if entry.kind == db.KIND_TEMPORARY]
        message_id = None
        if entries and channel_id:
            try:
                message = await self.bot.get_partial_messageable(channel_id).send(
                    view=daily_post_card(entries), allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                await db.record_daily_post(channel_id, None, retry=True)
                raise
            message_id = message.id
        await db.record_daily_post(channel_id, message_id)

    # ---- AI suggestions from solved tickets ----------------------------------------------------------------------

    async def suggest_from_ticket(
        self, suggestion, *, ticket_id: int | None, page_link: str | None = None
    ) -> db.SupportInfo:
        """Stores a ticket summary's SupportInfoSuggestion as 'suggested' and posts its review card to the review
        channel (support_info_review_channel_id, else moderation_log_channel_id)."""
        entry = await db.add_info(
            kind=suggestion.kind,
            title=suggestion.title,
            body=suggestion.body,
            created_by=None,
            status="suggested",
            source="ai",
            ticket_id=ticket_id,
        )
        settings = self.bot.settings
        channel_id = settings.support_info_review_channel_id or settings.moderation_log_channel_id
        if not channel_id:
            return entry
        message = await self.bot.get_partial_messageable(channel_id).send(
            view=review_card(entry, page_link=page_link, expiry_days=SUGGESTION_EXPIRY.days),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await db.set_review_message(entry.id, channel_id, message.id)
        return entry

    async def finish_suggestion(self, info_id: int, *, status: str, by_id: int) -> db.SupportInfo | None:
        """Approve ("active") or discard ("discarded") a suggestion and collapse its review card. None if someone
        else handled it first. Approving a temporary entry without an expiry gives it SUGGESTION_EXPIRY."""
        if status not in ("active", "discarded"):
            raise ValueError(f"Can't finish a suggestion as {status!r}.")
        default_expiry = datetime.now(timezone.utc) + SUGGESTION_EXPIRY if status == "active" else None
        entry = await db.set_status(
            info_id, status, updated_by=by_id, only_from=("suggested",), default_expires_at=default_expiry
        )
        if entry is not None:
            await self._collapse_review(entry)
        return entry

    async def _collapse_review(self, entry: db.SupportInfo) -> None:
        if not (entry.review_channel_id and entry.review_message_id):
            return
        message = self.bot.get_partial_messageable(entry.review_channel_id).get_partial_message(entry.review_message_id)
        try:
            await message.edit(view=review_handled_card(entry), allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound, discord.Forbidden):
            log.info("Could not edit support info review card", extra={"info_id": entry.id})
        except discord.HTTPException:
            # The decision is already saved; a card that didn't collapse must not undo it.
            log.exception("Failed to edit support info review card", extra={"info_id": entry.id})

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id") or ""
        if interaction.type == discord.InteractionType.modal_submit:
            if (message_id := feedback_fix.parse_modal_id(custom_id)) is not None:
                await self._on_fix_submit(interaction, message_id)
            return
        if interaction.type != discord.InteractionType.component:
            return
        if (fix := feedback_fix.parse_custom_id(custom_id)) is not None:
            return await self._on_fix_button(interaction, *fix)
        parsed = parse_review_custom_id(custom_id)
        if parsed is None:
            return
        action, info_id = parsed
        if not _is_staff_member(interaction.user, self.bot.settings):
            return await interaction.response.send_message(STAFF_ONLY_MSG, ephemeral=True)
        if action == EDIT:
            entry = await db.get_info(info_id)
            if entry is None or entry.status != "suggested":
                return await interaction.response.send_message(ALREADY_HANDLED_MSG, ephemeral=True)
            return await interaction.response.send_modal(SuggestionModal(self, entry))
        status = "active" if action == APPROVE else "discarded"

        async def finish() -> bool:
            if await self.finish_suggestion(info_id, status=status, by_id=interaction.user.id) is None:
                current = await db.get_info(info_id)
                if current is not None and current.status != "suggested":
                    await interaction.edit_original_response(view=review_handled_card(current))
                else:
                    await unbusy(interaction)
                await interaction.followup.send(ALREADY_HANDLED_MSG, ephemeral=True)
            return True

        await while_busy(interaction, self._working, finish, failed=REVIEW_FAILED_MSG)

    # ---- weekly "Answers to fix" (👎 answers; cogs/support_report.py posts the cards) ---------------------------------

    async def _fix_outcome(self, message_id: int) -> discord.ui.DesignerView | None:
        """The collapsed card of an answer someone already reviewed; None while nobody has."""
        row = await feedback_store.get(message_id)
        if row is None or row.reviewed_at is None:
            return None
        entry = await db.get_info(row.info_id) if row.info_id else None
        return feedback_fix.handled_card(row.reviewed_by, row.info_id, entry.title if entry else "")

    async def _on_fix_button(self, interaction: discord.Interaction, action: str, message_id: int) -> None:
        if not _is_staff_member(interaction.user, self.bot.settings):
            return await interaction.response.send_message(STAFF_ONLY_MSG, ephemeral=True)
        if action == feedback_fix.WRITE:
            row = await feedback_store.get(message_id)
            if row is None or row.reviewed_at is not None:
                outcome = await self._fix_outcome(message_id)
                if outcome is None:
                    return await interaction.response.send_message(FIX_HANDLED_MSG, ephemeral=True)
                await interaction.response.edit_message(view=outcome)
                return await interaction.followup.send(FIX_HANDLED_MSG, ephemeral=True)
            answer = await feedback_store.answer_to_fix(row)
            return await interaction.response.send_modal(
                feedback_fix.fix_modal(message_id, answer.question if answer else "")
            )

        async def skip() -> bool:
            if await feedback_store.claim_review(message_id, interaction.user.id):
                await interaction.edit_original_response(view=feedback_fix.skipped_card(interaction.user.id))
                return True
            outcome = await self._fix_outcome(message_id)
            if outcome is not None:
                await interaction.edit_original_response(view=outcome)
            else:
                await unbusy(interaction)
            await interaction.followup.send(FIX_HANDLED_MSG, ephemeral=True)
            return True

        await while_busy(interaction, self._working, skip, failed=FIX_FAILED_MSG)

    async def _on_fix_submit(self, interaction: discord.Interaction, message_id: int) -> None:
        """The Write form: claims the answer (first wins), saves the text as active support info (source feedback,
        created by the staff member) and collapses the card. A text that doesn't validate releases the claim."""
        if not _is_staff_member(interaction.user, self.bot.settings):
            return await interaction.response.send_message(STAFF_ONLY_MSG, ephemeral=True)
        form = feedback_fix.read_form((interaction.data or {}).get("components"))
        if form.days and not (form.days.isdigit() and 1 <= int(form.days) <= 365):
            return await interaction.response.send_message(DAYS_MSG, ephemeral=True)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=int(form.days))
            if form.days and form.kind == db.KIND_TEMPORARY
            else None
        )
        staff_id = interaction.user.id
        if not await feedback_store.claim_review(message_id, staff_id):
            return await interaction.response.send_message(FIX_HANDLED_MSG, ephemeral=True)
        try:
            entry = await db.add_info(
                kind=form.kind,
                title=form.title,
                body=form.answer,
                created_by=staff_id,
                expires_at=expires_at,
                status="active",
                source="feedback",
            )
        except Exception as error:
            if not isinstance(error, ValueError):
                log.exception("Could not save the right answer", extra={"message_id": message_id})
            await feedback_store.release_review(message_id, staff_id)
            reply = str(error) if isinstance(error, ValueError) else FIX_FAILED_MSG
            return await interaction.response.send_message(reply, ephemeral=True)
        try:
            await feedback_store.set_info(message_id, entry.id)
        except Exception:
            # The support info is live already; only the link back from the answer is missing.
            log.exception("Could not link the right answer to its feedback row", extra={"message_id": message_id})
        view = feedback_fix.fixed_card(staff_id, entry.id, entry.title)
        if getattr(interaction, "message", None) is not None:
            await interaction.response.edit_message(view=view)
        else:
            await interaction.response.send_message(f"Saved as support info #{entry.id}.", ephemeral=True)

    # ---- /info -----------------------------------------------------------------------------------------------------

    async def _staff(self, ctx: discord.ApplicationContext) -> bool:
        if _is_staff_member(ctx.author, self.bot.settings):
            return True
        await ctx.respond(STAFF_ONLY_MSG, ephemeral=True)
        return False

    @info.command(name="add", description="Add support info for the AI (temporary ones are also posted daily)")
    @discord.option("kind", str, description="temporary: a current issue; permanent: a lasting answer", choices=list(db.KINDS))
    @discord.option("days", int, description="Temporary only: expires after this many days", required=False, min_value=1, max_value=365)
    async def add(self, ctx: discord.ApplicationContext, kind: str, days: int | None = None):
        if not await self._staff(ctx):
            return
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=days) if days and kind == db.KIND_TEMPORARY else None
        )
        await ctx.send_modal(InfoModal(kind=kind, expires_at=expires_at))

    @info.command(name="edit", description="Edit support info")
    @discord.option("id", int, description="Support info number (see /info list)")
    async def edit(self, ctx: discord.ApplicationContext, id: int):
        if not await self._staff(ctx):
            return
        entry = await db.get_info(id)
        if entry is None or entry.status not in ("active", "suggested"):
            return await ctx.respond(NOT_FOUND_MSG.format(id=id), ephemeral=True)
        await ctx.send_modal(
            InfoModal(kind=entry.kind, expires_at=entry.expires_at, info_id=entry.id, title=entry.title, body=entry.body)
        )

    @info.command(name="list", description="List the active support info")
    async def list_(self, ctx: discord.ApplicationContext):
        if not await self._staff(ctx):
            return
        entries = await db.list_active()
        pending = len(await db.list_by_status("suggested"))
        await ctx.respond(view=info_list_card(entries, pending), ephemeral=True)

    @info.command(name="remove", description="Remove support info")
    @discord.option("id", int, description="Support info number (see /info list)")
    async def remove(self, ctx: discord.ApplicationContext, id: int):
        if not await self._staff(ctx):
            return
        entry = await db.set_status(id, "removed", updated_by=ctx.author.id, only_from=("active", "suggested"))
        if entry is None:
            return await ctx.respond(NOT_FOUND_MSG.format(id=id), ephemeral=True)
        await ctx.respond(f"Removed #{id}.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(SupportInfoCog(bot))
