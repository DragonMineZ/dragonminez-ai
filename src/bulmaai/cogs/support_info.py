"""Staff support info: /info to manage entries, the daily public post of active temporary ones, and the suggestions
the ticket close summary makes (suggest_from_ticket stores them; the panel's support info page approves or discards
them through finish_suggestion).

The post is DB-driven (support_info_post.posted_at) and checked every POLL_MINUTES, so restarts don't skip or repeat
it; claim_daily_post makes sure only one loop posts."""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks

from bulmaai.services import support_info as db
from bulmaai.ui.support_info_views import daily_post_card, info_list_card
from bulmaai.utils.i18n import described, reader_language, t, table
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import in_main_guild, is_staff

log = logging.getLogger(__name__)

POLL_MINUTES = 15
# Ephemeral replies and forms follow the reader; the cards in staff channels and the entries themselves stay English.
TEXT = table({
    "en": {
        "staff_only": "Only staff can manage support info.",
        "not_found": "No active support info #{id}.",
        "saved": "Saved #{id}.",
        "removed": "Removed #{id}.",
        "title": "Title",
        "text": "Text",
        "edit_heading": "Edit support info #{id}",
        "new_temporary": "New temporary support info",
        "new_permanent": "New permanent support info",
    },
    "es": {
        "staff_only": "Solo el staff puede gestionar la info de soporte.",
        "not_found": "No hay info de soporte activa #{id}.",
        "saved": "Guardada la #{id}.",
        "removed": "Se quitó la #{id}.",
        "title": "Título",
        "text": "Texto",
        "edit_heading": "Editar info de soporte #{id}",
        "new_temporary": "Nueva info de soporte temporal",
        "new_permanent": "Nueva info de soporte permanente",
    },
    "pt": {
        "staff_only": "Só a staff pode gerenciar as infos de suporte.",
        "not_found": "Não há info de suporte ativa #{id}.",
        "saved": "#{id} salva.",
        "removed": "#{id} removida.",
        "title": "Título",
        "text": "Texto",
        "edit_heading": "Editar info de suporte #{id}",
        "new_temporary": "Nova info de suporte temporária",
        "new_permanent": "Nova info de suporte permanente",
    },
})
DESC = table({
    "en": {
        "info": "Support info the AI uses and the daily known-issues post",
        "add": "Add support info for the AI (temporary ones are also posted daily)",
        "add.kind": "temporary: a current issue; permanent: a lasting answer",
        "add.days": "Temporary only: expires after this many days",
        "edit": "Edit support info",
        "id": "Support info number (see /info list)",
        "list": "List the active support info",
        "remove": "Remove support info",
    },
    "es": {
        "info": "Info de soporte que usa la IA y el post diario de problemas conocidos",
        "add": "Agrega info de soporte para la IA (las temporales también se publican a diario)",
        "add.kind": "temporary: un problema actual; permanent: una respuesta que se queda",
        "add.days": "Solo temporales: expira después de estos días",
        "edit": "Edita info de soporte",
        "id": "Número de la info de soporte (mira /info list)",
        "list": "Lista la info de soporte activa",
        "remove": "Quita info de soporte",
    },
    "pt": {
        "info": "Infos de suporte que a IA usa e o post diário de problemas conhecidos",
        "add": "Adicione info de suporte para a IA (as temporárias também são postadas todo dia)",
        "add.kind": "temporary: um problema atual; permanent: uma resposta duradoura",
        "add.days": "Só temporárias: expira depois de tantos dias",
        "edit": "Edite uma info de suporte",
        "id": "Número da info de suporte (veja /info list)",
        "list": "Liste as infos de suporte ativas",
        "remove": "Remova uma info de suporte",
    },
})
# English copy the tests compare against.
STAFF_ONLY_MSG = TEXT["en"]["staff_only"]


def _say(interaction, key: str, **values) -> str:
    """TEXT[key] in the language of whoever clicked or ran the command."""
    return t(TEXT, key, reader_language(interaction), **values)
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
        language: str = "en",
    ) -> None:
        self.title_input = discord.ui.InputText(
            style=discord.InputTextStyle.short, max_length=db.TITLE_MAX, value=title or None
        )
        self.body_input = discord.ui.InputText(
            style=discord.InputTextStyle.paragraph, max_length=db.BODY_MAX, value=body or None
        )
        heading = t(TEXT, "edit_heading", language, id=info_id) if info_id else t(TEXT, f"new_{kind}", language)
        super().__init__(
            discord.ui.Label(t(TEXT, "title", language), self.title_input),
            discord.ui.Label(t(TEXT, "text", language), self.body_input),
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
            return await interaction.response.send_message(_say(interaction, "not_found", id=self.info_id), ephemeral=True)
        await interaction.response.send_message(_say(interaction, "saved", id=entry.id), ephemeral=True)


class SupportInfoCog(ReloadableCog):
    info = discord.SlashCommandGroup("info", **described(DESC, "info"))

    def __init__(self, bot: discord.Bot):
        self.bot = bot

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

    async def suggest_from_ticket(self, suggestion, *, ticket_id: int | None) -> db.SupportInfo:
        """Stores a ticket summary's SupportInfoSuggestion as 'suggested'; staff review it on the panel's support
        info page."""
        return await db.add_info(
            kind=suggestion.kind,
            title=suggestion.title,
            body=suggestion.body,
            created_by=None,
            status="suggested",
            source="ai",
            ticket_id=ticket_id,
        )

    async def finish_suggestion(self, info_id: int, *, status: str, by_id: int) -> db.SupportInfo | None:
        """Approve ("active") or discard ("discarded") a suggestion. None if someone else handled it first. Approving
        a temporary entry without an expiry gives it SUGGESTION_EXPIRY."""
        if status not in ("active", "discarded"):
            raise ValueError(f"Can't finish a suggestion as {status!r}.")
        default_expiry = datetime.now(timezone.utc) + SUGGESTION_EXPIRY if status == "active" else None
        return await db.set_status(
            info_id, status, updated_by=by_id, only_from=("suggested",), default_expires_at=default_expiry
        )

    # ---- /info -----------------------------------------------------------------------------------------------------

    async def _staff(self, ctx: discord.ApplicationContext) -> bool:
        if _is_staff_member(ctx.author, self.bot.settings):
            return True
        await ctx.respond(_say(ctx, "staff_only"), ephemeral=True)
        return False

    @info.command(name="add", **described(DESC, "add"))
    @discord.option("kind", str, **described(DESC, "add.kind"), choices=list(db.KINDS))
    @discord.option("days", int, **described(DESC, "add.days"), required=False, min_value=1, max_value=365)
    async def add(self, ctx: discord.ApplicationContext, kind: str, days: int | None = None):
        if not await self._staff(ctx):
            return
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=days) if days and kind == db.KIND_TEMPORARY else None
        )
        await ctx.send_modal(InfoModal(kind=kind, expires_at=expires_at, language=reader_language(ctx)))

    @info.command(name="edit", **described(DESC, "edit"))
    @discord.option("id", int, **described(DESC, "id"))
    async def edit(self, ctx: discord.ApplicationContext, id: int):
        if not await self._staff(ctx):
            return
        entry = await db.get_info(id)
        if entry is None or entry.status not in ("active", "suggested"):
            return await ctx.respond(_say(ctx, "not_found", id=id), ephemeral=True)
        await ctx.send_modal(
            InfoModal(
                kind=entry.kind,
                expires_at=entry.expires_at,
                info_id=entry.id,
                title=entry.title,
                body=entry.body,
                language=reader_language(ctx),
            )
        )

    @info.command(name="list", **described(DESC, "list"))
    async def list_(self, ctx: discord.ApplicationContext):
        if not await self._staff(ctx):
            return
        entries = await db.list_active()
        pending = len(await db.list_by_status("suggested"))
        await ctx.respond(view=info_list_card(entries, pending), ephemeral=True)

    @info.command(name="remove", **described(DESC, "remove"))
    @discord.option("id", int, **described(DESC, "id"))
    async def remove(self, ctx: discord.ApplicationContext, id: int):
        if not await self._staff(ctx):
            return
        entry = await db.set_status(id, "removed", updated_by=ctx.author.id, only_from=("active", "suggested"))
        if entry is None:
            return await ctx.respond(_say(ctx, "not_found", id=id), ephemeral=True)
        await ctx.respond(_say(ctx, "removed", id=id), ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(SupportInfoCog(bot))
