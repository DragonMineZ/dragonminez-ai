"""Dyno-style moderation slash commands and context menus. Every action goes through mod_actions.perform();
access follows the panel's staff tiers (web/core.py PERMISSIONS), default_permissions only hides the commands."""

import asyncio
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from bulmaai.services import member_activity, mod_actions, mod_cases, panel_logs, patreon_grants, scam_images
from bulmaai.services import tickets as ticket_store
from bulmaai.services.mod_actions import (
    MAX_TIMEOUT_SECONDS,
    ActionResult,
    LadderStep,
    ModActionError,
    format_duration,
    parse_duration_seconds,
    parse_ladder,
    pick_step,
    warn_expiry_seconds,
)
from bulmaai.ui.mod_cards import reply_card
from bulmaai.ui.v2 import fit, trim
from bulmaai.ui.v2 import head as v2_head
from bulmaai.utils.i18n import described, reader_language, t, table
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.web.core import PERMISSIONS, resolve_member, tier_for

log = logging.getLogger(__name__)

MAX_REASON_LENGTH = 400  # the panel's cap; leaves room for the audit-log suffix under Discord's 512
BAN_DELETE_SECONDS = {"none": 0, "1h": 3600, "24h": 86400, "7d": 7 * 86400}
STAFF_ONLY = discord.Permissions(moderate_members=True)
# Dyno-style confirmation: emoji, "<name> <phrase>", accent colour.
VERBS = {
    "warn": ("⚠️", "has been warned", discord.Color.gold()),
    "note": ("📝", "has a new staff note", discord.Color.blurple()),
    "timeout": ("🔇", "has been timed out", discord.Color.orange()),
    "untimeout": ("🔊", "is no longer timed out", discord.Color.green()),
    "kick": ("👢", "has been kicked", discord.Color.dark_orange()),
    "ban": ("🔨", "has been banned", discord.Color.red()),
    "softban": ("🧹", "has been softbanned", discord.Color.dark_orange()),
    "unban": ("🕊️", "has been unbanned", discord.Color.green()),
}
WARNINGS_PER_PAGE = 5
NO_PINGS = discord.AllowedMentions.none()

# What only the moderator sees (errors, confirm prompts, forms) is in their language; public results and the mod log
# stay English, since they post in channels other staff read too.
TEXT = table({
    "en": {
        "tier_denied": "Your staff tier can't do that.",
        "cancel": "Cancel",
        "cancelled": "Cancelled.",
        "working": "⏳ Working…",
        "reason": "Reason",
        "already_removed": "Case #{case_id} was already removed.",
        "not_a": "Case #{case_id} isn't a {label}.",
        "label_warn": "warning",
        "label_note": "note",
        "clear_prompt": "**Clear all active warnings for <@{user_id}>?**",
        "clear_button": "Clear warnings",
        "bad_duration": "Couldn't read that duration, try 30m, 2h or 1d.",
        "bad_ban_duration": "Couldn't read that duration, try 12h, 7d or 2w (leave it empty for a permanent ban).",
        "bad_user_id": "That isn't a valid user ID.",
        "purge_refused": "Discord refused: the bot can't delete messages here.",
        "purge_from": "from <@{user_id}>",
        "purge_containing": 'containing "{text}"',
        "purge_some": "Scan the last **{amount}** message(s) here and delete the ones {filters}?",
        "purge_all": "Scan the last **{amount}** message(s) here and delete all of them?",
        "purge_button": "Purge",
        "unknown_case": "Unknown case.",
        "no_case": "There's no case #{case_id}. Check /modlogs for the user's case numbers.",
        "thread_lock": "Threads can't be locked this way; lock the parent channel.",
        "thread_unlock": "Threads can't be unlocked this way; unlock the parent channel.",
        "channel_refused": "Discord refused: the bot can't edit that channel's permissions.",
        "slowmode_refused": "Discord refused: the bot can't edit that channel.",
        "lockdown_prompt": "**Lock every public channel** (or the configured lockdown list)?",
        "lockdown_button": "Start lockdown",
        "warn_title": "Warn {name}",
        "delete_warn_title": "Delete & warn",
        "warned_not_deleted": "Warned, but couldn't delete the message.",
        "no_images": "No usable images on that message.",
        "db_down": "Couldn't save, the database is unavailable.",
    },
    "es": {
        "tier_denied": "Tu nivel de staff no puede hacer eso.",
        "cancel": "Cancelar",
        "cancelled": "Cancelado.",
        "working": "⏳ Trabajando…",
        "reason": "Motivo",
        "already_removed": "El caso #{case_id} ya fue eliminado.",
        "not_a": "El caso #{case_id} no es una {label}.",
        "label_warn": "advertencia",
        "label_note": "nota",
        "clear_prompt": "**¿Borrar todas las advertencias activas de <@{user_id}>?**",
        "clear_button": "Borrar advertencias",
        "bad_duration": "No entendí esa duración, prueba 30m, 2h o 1d.",
        "bad_ban_duration": "No entendí esa duración, prueba 12h, 7d o 2w (déjala vacía para un baneo permanente).",
        "bad_user_id": "Ese no es un ID de usuario válido.",
        "purge_refused": "Discord lo rechazó: el bot no puede borrar mensajes aquí.",
        "purge_from": "de <@{user_id}>",
        "purge_containing": 'que contengan "{text}"',
        "purge_some": "¿Revisar los últimos **{amount}** mensajes de aquí y borrar los {filters}?",
        "purge_all": "¿Revisar los últimos **{amount}** mensajes de aquí y borrarlos todos?",
        "purge_button": "Borrar",
        "unknown_case": "Caso desconocido.",
        "no_case": "No existe el caso #{case_id}. Revisa /modlogs para ver los números de caso del usuario.",
        "thread_lock": "Los hilos no se pueden bloquear así; bloquea el canal principal.",
        "thread_unlock": "Los hilos no se pueden desbloquear así; desbloquea el canal principal.",
        "channel_refused": "Discord lo rechazó: el bot no puede editar los permisos de ese canal.",
        "slowmode_refused": "Discord lo rechazó: el bot no puede editar ese canal.",
        "lockdown_prompt": "¿**Bloquear todos los canales públicos** (o la lista de bloqueo configurada)?",
        "lockdown_button": "Iniciar bloqueo",
        "warn_title": "Advertir a {name}",
        "delete_warn_title": "Borrar y advertir",
        "warned_not_deleted": "Advertido, pero no pude borrar el mensaje.",
        "no_images": "Ese mensaje no tiene imágenes utilizables.",
        "db_down": "No pude guardar, la base de datos no está disponible.",
    },
    "pt": {
        "tier_denied": "Seu nível de staff não pode fazer isso.",
        "cancel": "Cancelar",
        "cancelled": "Cancelado.",
        "working": "⏳ Trabalhando…",
        "reason": "Motivo",
        "already_removed": "O caso #{case_id} já foi removido.",
        "not_a": "O caso #{case_id} não é uma {label}.",
        "label_warn": "advertência",
        "label_note": "nota",
        "clear_prompt": "**Limpar todas as advertências ativas de <@{user_id}>?**",
        "clear_button": "Limpar advertências",
        "bad_duration": "Não entendi essa duração, tente 30m, 2h ou 1d.",
        "bad_ban_duration": "Não entendi essa duração, tente 12h, 7d ou 2w (deixe vazio para um banimento permanente).",
        "bad_user_id": "Esse não é um ID de usuário válido.",
        "purge_refused": "O Discord recusou: o bot não pode apagar mensagens aqui.",
        "purge_from": "de <@{user_id}>",
        "purge_containing": 'que contenham "{text}"',
        "purge_some": "Verificar as últimas **{amount}** mensagens daqui e apagar as {filters}?",
        "purge_all": "Verificar as últimas **{amount}** mensagens daqui e apagar todas?",
        "purge_button": "Apagar",
        "unknown_case": "Caso desconhecido.",
        "no_case": "Não existe o caso #{case_id}. Veja /modlogs para os números de caso do usuário.",
        "thread_lock": "Tópicos não podem ser bloqueados assim; bloqueie o canal principal.",
        "thread_unlock": "Tópicos não podem ser desbloqueados assim; desbloqueie o canal principal.",
        "channel_refused": "O Discord recusou: o bot não pode editar as permissões desse canal.",
        "slowmode_refused": "O Discord recusou: o bot não pode editar esse canal.",
        "lockdown_prompt": "**Bloquear todos os canais públicos** (ou a lista de bloqueio configurada)?",
        "lockdown_button": "Iniciar bloqueio",
        "warn_title": "Advertir {name}",
        "delete_warn_title": "Apagar e advertir",
        "warned_not_deleted": "Advertido, mas não consegui apagar a mensagem.",
        "no_images": "Essa mensagem não tem imagens utilizáveis.",
        "db_down": "Não consegui salvar, o banco de dados está indisponível.",
    },
})
# Command and option descriptions per Discord client language (names stay English).
DESC = table({
    "en": {
        "lockdown": "Lock or unlock every public channel",
        "warn": "Warn a member (counts toward the warn ladder)",
        "warn.user": "Member to warn",
        "dm_reason": "Shown to them in the DM",
        "warnings": "A member's active warnings and warn-ladder standing",
        "lookup_member": "Member to look up",
        "lookup_user": "User to look up",
        "delwarn": "Remove a warning (it stops counting toward the ladder)",
        "delwarn.case_id": "Warning case number",
        "delnote": "Remove a staff note",
        "delnote.case_id": "Note case number",
        "clearwarns": "Remove all of a member's active warnings",
        "clearwarns.user": "Member whose warnings to clear",
        "note": "Add a staff-only note to a user",
        "note.user": "User to note",
        "note.text": "The note",
        "notes": "A user's staff notes",
        "mute": "Time a member out",
        "mute.user": "Member to time out",
        "mute.duration": "e.g. 30m, 2h, 1d (max 28d)",
        "unmute": "Remove a member's timeout",
        "unmute.user": "Member to un-time-out",
        "optional": "Optional",
        "why": "Why",
        "kick": "Kick a member",
        "kick.user": "Member to kick",
        "ban": "Ban a user, optionally for a limited time",
        "ban.user": "User to ban (IDs of non-members work)",
        "ban.duration": "e.g. 12h, 7d, 2w; empty = permanent",
        "ban.delete_messages": "Delete their recent messages (default 7d)",
        "softban": "Ban and unban to delete a day of messages",
        "softban.user": "User to softban",
        "unban": "Unban a user by ID",
        "unban.user_id": "Their Discord user ID",
        "purge": "Bulk-delete recent messages in this channel",
        "purge.amount": "How many recent messages to scan",
        "purge.user": "Only this user's messages",
        "purge.contains": "Only messages containing this text",
        "purge.kind": "Only this kind of message",
        "case": "Show a moderation case",
        "case.case_id": "Case number",
        "reason": "Fix the reason on an existing case (find case numbers with /modlogs or /warnings)",
        "reason.case_id": "The case number, e.g. 42 for case #42",
        "reason.reason": "Replaces the old reason; the user isn't DMed again",
        "modlogs": "A user's last 25 moderation cases",
        "modstats": "Moderation actions per moderator",
        "modstats.days": "Look back this many days (default 30)",
        "lock": "Stop @everyone from talking in a channel",
        "this_channel": "Default: this channel",
        "unlock": "Undo /lock on a channel",
        "lockdown.start": "Lock every public channel (or the configured list)",
        "lockdown.end": "Unlock every channel the bot locked",
        "slowmode": "Set a channel's slowmode",
        "slowmode.seconds": "0 turns it off (max 21600 = 6h)",
        "whois": "Who a user is: account, roles, moderation record, tickets",
    },
    "es": {
        "lockdown": "Bloquea o desbloquea todos los canales públicos",
        "warn": "Advierte a un miembro (cuenta para la escalera de advertencias)",
        "warn.user": "Miembro a advertir",
        "dm_reason": "Se le muestra en el MD",
        "warnings": "Advertencias activas de un miembro y su lugar en la escalera",
        "lookup_member": "Miembro a consultar",
        "lookup_user": "Usuario a consultar",
        "delwarn": "Quita una advertencia (deja de contar para la escalera)",
        "delwarn.case_id": "Número de caso de la advertencia",
        "delnote": "Quita una nota del staff",
        "delnote.case_id": "Número de caso de la nota",
        "clearwarns": "Quita todas las advertencias activas de un miembro",
        "clearwarns.user": "Miembro cuyas advertencias borrar",
        "note": "Añade una nota solo para el staff a un usuario",
        "note.user": "Usuario de la nota",
        "note.text": "La nota",
        "notes": "Las notas del staff de un usuario",
        "mute": "Da un timeout a un miembro",
        "mute.user": "Miembro al que dar timeout",
        "mute.duration": "p. ej. 30m, 2h, 1d (máx. 28d)",
        "unmute": "Quita el timeout de un miembro",
        "unmute.user": "Miembro al que quitar el timeout",
        "optional": "Opcional",
        "why": "Por qué",
        "kick": "Expulsa a un miembro",
        "kick.user": "Miembro a expulsar",
        "ban": "Banea a un usuario, opcionalmente por un tiempo",
        "ban.user": "Usuario a banear (sirven IDs de no miembros)",
        "ban.duration": "p. ej. 12h, 7d, 2w; vacío = permanente",
        "ban.delete_messages": "Borra sus mensajes recientes (por defecto 7d)",
        "softban": "Banea y desbanea para borrar un día de mensajes",
        "softban.user": "Usuario a softbanear",
        "unban": "Desbanea a un usuario por ID",
        "unban.user_id": "Su ID de usuario de Discord",
        "purge": "Borra en masa los mensajes recientes de este canal",
        "purge.amount": "Cuántos mensajes recientes revisar",
        "purge.user": "Solo los mensajes de este usuario",
        "purge.contains": "Solo los mensajes que contengan este texto",
        "purge.kind": "Solo este tipo de mensaje",
        "case": "Muestra un caso de moderación",
        "case.case_id": "Número de caso",
        "reason": "Corrige el motivo de un caso (busca los números con /modlogs o /warnings)",
        "reason.case_id": "El número de caso, p. ej. 42 para el caso #42",
        "reason.reason": "Reemplaza el motivo anterior; no se le vuelve a enviar MD",
        "modlogs": "Los últimos 25 casos de moderación de un usuario",
        "modstats": "Acciones de moderación por moderador",
        "modstats.days": "Cuántos días mirar atrás (por defecto 30)",
        "lock": "Impide que @everyone hable en un canal",
        "this_channel": "Por defecto: este canal",
        "unlock": "Deshace /lock en un canal",
        "lockdown.start": "Bloquea todos los canales públicos (o la lista configurada)",
        "lockdown.end": "Desbloquea todos los canales que bloqueó el bot",
        "slowmode": "Configura el modo lento de un canal",
        "slowmode.seconds": "0 lo desactiva (máx. 21600 = 6h)",
        "whois": "Quién es un usuario: cuenta, roles, historial de moderación, tickets",
    },
    "pt": {
        "lockdown": "Bloqueia ou desbloqueia todos os canais públicos",
        "warn": "Adverte um membro (conta para a escada de advertências)",
        "warn.user": "Membro a advertir",
        "dm_reason": "Mostrado a ele na DM",
        "warnings": "Advertências ativas de um membro e sua posição na escada",
        "lookup_member": "Membro a consultar",
        "lookup_user": "Usuário a consultar",
        "delwarn": "Remove uma advertência (ela deixa de contar para a escada)",
        "delwarn.case_id": "Número do caso da advertência",
        "delnote": "Remove uma nota da staff",
        "delnote.case_id": "Número do caso da nota",
        "clearwarns": "Remove todas as advertências ativas de um membro",
        "clearwarns.user": "Membro cujas advertências limpar",
        "note": "Adiciona uma nota só para a staff a um usuário",
        "note.user": "Usuário da nota",
        "note.text": "A nota",
        "notes": "As notas da staff sobre um usuário",
        "mute": "Dá um timeout a um membro",
        "mute.user": "Membro que vai receber o timeout",
        "mute.duration": "ex.: 30m, 2h, 1d (máx. 28d)",
        "unmute": "Remove o timeout de um membro",
        "unmute.user": "Membro que vai perder o timeout",
        "optional": "Opcional",
        "why": "Por quê",
        "kick": "Expulsa um membro",
        "kick.user": "Membro a expulsar",
        "ban": "Bane um usuário, opcionalmente por um tempo",
        "ban.user": "Usuário a banir (IDs de não membros funcionam)",
        "ban.duration": "ex.: 12h, 7d, 2w; vazio = permanente",
        "ban.delete_messages": "Apaga as mensagens recentes dele (padrão 7d)",
        "softban": "Bane e desbane para apagar um dia de mensagens",
        "softban.user": "Usuário a dar softban",
        "unban": "Desbane um usuário pelo ID",
        "unban.user_id": "O ID de usuário do Discord dele",
        "purge": "Apaga em massa as mensagens recentes deste canal",
        "purge.amount": "Quantas mensagens recentes verificar",
        "purge.user": "Só as mensagens deste usuário",
        "purge.contains": "Só mensagens que contenham este texto",
        "purge.kind": "Só este tipo de mensagem",
        "case": "Mostra um caso de moderação",
        "case.case_id": "Número do caso",
        "reason": "Corrige o motivo de um caso (ache os números com /modlogs ou /warnings)",
        "reason.case_id": "O número do caso, ex.: 42 para o caso #42",
        "reason.reason": "Substitui o motivo antigo; o usuário não recebe outra DM",
        "modlogs": "Os últimos 25 casos de moderação de um usuário",
        "modstats": "Ações de moderação por moderador",
        "modstats.days": "Quantos dias olhar para trás (padrão 30)",
        "lock": "Impede que @everyone fale em um canal",
        "this_channel": "Padrão: este canal",
        "unlock": "Desfaz o /lock em um canal",
        "lockdown.start": "Bloqueia todos os canais públicos (ou a lista configurada)",
        "lockdown.end": "Desbloqueia todos os canais que o bot bloqueou",
        "slowmode": "Define o modo lento de um canal",
        "slowmode.seconds": "0 desativa (máx. 21600 = 6h)",
        "whois": "Quem é um usuário: conta, cargos, histórico de moderação, tickets",
    },
})

_LINK_RE = re.compile(r"https?://", re.IGNORECASE)
_INVITE_RE = re.compile(r"discord(?:\.gg|(?:app)?\.com/invite)/", re.IGNORECASE)
PURGE_KINDS: dict[str, Callable[[discord.Message], bool]] = {
    "bots": lambda m: m.author.bot,
    "humans": lambda m: not m.author.bot,
    "links": lambda m: bool(_LINK_RE.search(m.content)),
    "images": lambda m: any((a.content_type or "").startswith("image/") for a in m.attachments),
    "attachments": lambda m: bool(m.attachments),
    "embeds": lambda m: bool(m.embeds),
    "invites": lambda m: bool(_INVITE_RE.search(m.content)),
}


def purge_check(
    *, user_id: int | None = None, contains: str | None = None, kind: str | None = None
) -> Callable[[discord.Message], bool]:
    """All filters ANDed; pinned messages are always kept."""
    needle = (contains or "").lower()
    matches_kind = PURGE_KINDS[kind] if kind else lambda _message: True

    def check(message: discord.Message) -> bool:
        return (
            not message.pinned
            and (user_id is None or message.author.id == user_id)
            and needle in message.content.lower()
            and matches_kind(message)
        )

    return check


def _step_text(step: LadderStep) -> str:
    return f"{step.action} {format_duration(step.duration_seconds)}" if step.duration_seconds else step.action


def _ladder_bar(count: int, needed: int) -> str:
    filled = min(count, needed, 10)
    return "▰" * filled + "▱" * (min(needed, 10) - filled)


def _case_lines(cases: list[mod_cases.ModCase]) -> str:
    lines = []
    for case in cases:
        reason = (case.reason or "no reason").replace("\n", " ")
        reason = discord.utils.escape_markdown(reason if len(reason) <= 60 else reason[:59] + "…")
        line = f"`#{case.id}` **{case.action}** · {case.source} · {discord.utils.format_dt(case.created_at, 'R')} · {reason}"
        lines.append(line if case.active else f"~~{line}~~")
    return "\n".join(lines)[:4096]


# The permissions worth knowing about at a glance on /whois (Administrator alone replaces the list).
KEY_PERMISSIONS = (
    ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"),
    ("manage_channels", "Manage Channels"),
    ("ban_members", "Ban"),
    ("kick_members", "Kick"),
    ("moderate_members", "Timeout"),
    ("manage_messages", "Manage Messages"),
    ("mention_everyone", "Mention `@everyone`"),
    ("manage_webhooks", "Manage Webhooks"),
)
NEW_ACCOUNT_DAYS = 7


def _whois_flags(user: discord.abc.User, member: discord.Member | None, now) -> list[str]:
    """Things a moderator wants to notice first."""
    flags = []
    age = now - user.created_at
    if age < timedelta(days=NEW_ACCOUNT_DAYS):
        flags.append(f"🆕 New account, {format_duration(max(int(age.total_seconds()), 1))} old")
    if member is not None and member.joined_at and now - member.joined_at < timedelta(days=1):
        flags.append("🚪 Joined in the last 24h")
    if member is not None and member.pending:
        flags.append("⏳ Hasn't passed membership screening")
    if user.avatar is None:
        flags.append("🖼️ Default avatar")
    if user.bot:
        flags.append("🤖 Bot account")
    return flags


async def _quietly(awaitable):
    """The extra /whois facts are best effort: a failed lookup drops its line, not the card."""
    try:
        return await awaitable
    except Exception:
        log.warning("A /whois lookup failed", exc_info=True)
        return None


class ReasonModal(discord.ui.Modal):
    def __init__(self, title: str, submit: Callable[[discord.Interaction, str], Awaitable[object]], language: str = "en"):
        super().__init__(title=title[:45])
        self.reason_input = discord.ui.InputText(
            label=t(TEXT, "reason", language), style=discord.InputTextStyle.long, max_length=300
        )
        self.add_item(self.reason_input)
        self._submit = submit

    async def callback(self, interaction: discord.Interaction):
        await self._submit(interaction, self.reason_input.value.strip())


class ConfirmView(discord.ui.DesignerView):
    """Private "are you sure?" card. On confirm it turns into "Working…" and on_confirm runs;
    on_confirm posts its own (public) result with interaction.followup.send."""

    def __init__(
        self,
        prompt: str,
        label: str,
        on_confirm: Callable[[discord.Interaction], Awaitable[object]],
        language: str = "en",
    ):
        confirm = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label=t(TEXT, "cancel", language), style=discord.ButtonStyle.secondary)
        confirm.callback, cancel.callback = self._confirm, self._cancel
        super().__init__(
            discord.ui.Container(
                discord.ui.TextDisplay(prompt), discord.ui.ActionRow(confirm, cancel), color=discord.Color.gold()
            ),
            timeout=120,
            disable_on_timeout=True,
        )
        self._on_confirm = on_confirm
        self._language = language

    async def _confirm(self, interaction: discord.Interaction):
        self.stop()
        working = reply_card(t(TEXT, "working", self._language), discord.Color.dark_grey())
        await interaction.response.edit_message(view=working)
        await self._on_confirm(interaction)
        await interaction.delete_original_response()

    async def _cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(
            view=reply_card(t(TEXT, "cancelled", self._language), discord.Color.dark_grey())
        )


class ModCommandsCog(ReloadableCog):
    lockdown_group = discord.SlashCommandGroup(
        "lockdown", **described(DESC, "lockdown"), default_member_permissions=STAFF_ONLY
    )

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    def _say(self, ctx, key: str, **values) -> str:
        """TEXT[key] in the language of whoever ran ctx (a command's context or an interaction)."""
        return t(TEXT, key, reader_language(ctx, self.bot.settings), **values)

    async def _allowed(self, ctx: discord.ApplicationContext | discord.Interaction, capability: str) -> bool:
        if tier_for(ctx.user, self.bot.settings) >= PERMISSIONS[capability]:
            return True
        await ctx.respond(self._say(ctx, "tier_denied"), ephemeral=True)
        return False

    @staticmethod
    async def _private_error(ctx: discord.ApplicationContext, text: str) -> None:
        """An error after a public defer: drop the public "thinking…" so only the moderator sees it."""
        interaction = getattr(ctx, "interaction", ctx)
        if interaction.response.is_done():
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
            await ctx.followup.send(text, ephemeral=True)
        else:
            await ctx.respond(text, ephemeral=True)

    async def _post_mod_log(self, kind: str, title: str, text: str, *, user_id: int, data: dict | None = None) -> None:
        """Best effort: the panel log plus a grey card in the moderation log channel."""
        try:
            await panel_logs.record(kind, title, text, user_id=user_id, data=data)
            channel = await mod_actions.resolve_channel(self.bot, mod_actions.mod_log_channel_id(self.bot.settings))
            if channel is not None:
                card = reply_card(f"### {title}\n{text}"[:3800], discord.Color.dark_grey())
                await channel.send(view=card, allowed_mentions=NO_PINGS)
        except Exception:
            log.warning("Failed to post %s to the moderation log", kind, exc_info=True)

    async def _display_name(self, user_id: int) -> str:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return f"<@{user_id}>"
        return discord.utils.escape_markdown(user.name)

    async def _perform(self, ctx, action: str, target_id: int, reason: str, **options) -> ActionResult | None:
        """ctx is an ApplicationContext or a modal's Interaction; replies to it either way.
        Confirmations are public like Dyno's (the reason isn't shown); errors stay private."""
        interaction = getattr(ctx, "interaction", ctx)
        # DM + Discord call + DB + mod-log post (+ a ladder step) can outlast the 3s interaction window.
        deferred_here = not interaction.response.is_done()
        if deferred_here:
            await interaction.response.defer()
        try:
            result = await mod_actions.perform(
                self.bot, ctx.guild, action=action, target_id=target_id, moderator=ctx.user, reason=reason, **options
            )
        except ModActionError as error:
            if deferred_here:
                await interaction.delete_original_response()  # drop the public "thinking…" so the error stays private
            await interaction.followup.send(f"❌ {error.text(reader_language(ctx, self.bot.settings))}", ephemeral=True)
            return None
        emoji, phrase, color = VERBS[action]
        text = f"{emoji} ***{await self._display_name(target_id)} {phrase}"
        if options.get("duration_seconds"):
            text += f" for {format_duration(options['duration_seconds'])}"
        text += ".***"
        if escalation := result.escalation:
            duration = escalation.duration_seconds
            text += f"\n⚡ Warn ladder kicked in: **{escalation.action}"
            text += (f" {format_duration(duration)}" if duration else "") + "**"
            text += f" (case #{escalation.case_id})" if escalation.case_id else ""
        if result.ladder_skipped:
            text += f"\n⚠️ Warn ladder: {result.ladder_skipped}"
        footer = f"Case #{result.case_id}" if result.case_id else "The case couldn't be recorded"
        await ctx.respond(view=reply_card(text, color, footer), allowed_mentions=NO_PINGS)
        return result

    # --- warnings and notes ------------------------------------------------------------------

    @discord.slash_command(name="warn", **described(DESC, "warn"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "warn.user"))
    @discord.option("reason", str, **described(DESC, "dm_reason"), max_length=MAX_REASON_LENGTH)
    async def warn(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.warn"):
            await self._perform(ctx, "warn", user.id, reason)

    @discord.slash_command(name="warnings", **described(DESC, "warnings"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "lookup_member"))
    async def warnings(self, ctx: discord.ApplicationContext, user: discord.User):
        if await self._allowed(ctx, "mod.cases.view"):
            await ctx.respond(view=await self._warnings_view(ctx.guild.id, user), allowed_mentions=NO_PINGS)

    @staticmethod
    async def _warn_counts(guild_id: int, user_id: int, steps: list[LadderStep]) -> dict[int, int]:
        """Active warns per ladder window (0 = all time)."""
        now = discord.utils.utcnow()
        return {
            window: await mod_cases.count_active_since(
                guild_id, user_id, "warn", now - timedelta(seconds=window) if window else None
            )
            for window in {step.window_seconds for step in steps}
        }

    async def _ladder_text(self, guild_id: int, user_id: int) -> str | None:
        steps = parse_ladder(self.bot.settings.moderation_warn_ladder)
        if not steps:
            return None
        counts = await self._warn_counts(guild_id, user_id, steps)
        expiry = getattr(self.bot.settings, "moderation_warn_expiry", "")
        lines = []
        for step in steps:
            count = counts[step.window_seconds]
            window = f" in {format_duration(step.window_seconds)}" if step.window_seconds else ""
            lasts = warn_expiry_seconds(expiry, step.warns)
            tail = f" · expires after {format_duration(lasts)}" if lasts else ""
            lines.append(
                f"`{_ladder_bar(count, step.warns)}` **{count}/{step.warns}** warns{window} → {_step_text(step)}{tail}"
            )
        upcoming = pick_step(steps, {window: count + 1 for window, count in counts.items()})
        lines.append(f"-# Next warn → {f'**{_step_text(upcoming)}**' if upcoming else 'no automatic action'}")
        return "### Warn ladder\n" + "\n".join(lines)

    async def _warnings_view(self, guild_id: int, user: discord.abc.User, page: int = 0) -> discord.ui.DesignerView:
        """Components V2 card in Dyno's layout: one row per warning with a delete button, then the ladder."""
        warns = [c for c in await mod_cases.list_cases(guild_id, user_id=user.id, action="warn", limit=100) if c.active]
        pages = max(1, -(-len(warns) // WARNINGS_PER_PAGE))
        page = max(0, min(page, pages - 1))
        items: list[discord.ui.Item] = [
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"## Warnings for {discord.utils.escape_markdown(user.name)}\n-# {user.mention} · `{user.id}`"
                ),
                accessory=discord.ui.Thumbnail(user.display_avatar.url),
            ),
            discord.ui.Separator(),
        ]
        for case in warns[page * WARNINGS_PER_PAGE : (page + 1) * WARNINGS_PER_PAGE]:
            reason = (case.reason or "No reason").replace("\n", " ")
            reason = discord.utils.escape_markdown(reason if len(reason) <= 200 else reason[:199] + "…")
            moderator = f"<@{case.moderator_id}>" if case.moderator_id else case.source
            delete = discord.ui.Button(emoji="🗑️", style=discord.ButtonStyle.danger)
            delete.callback = self._warn_delete_callback(guild_id, user, case.id, page)
            when = discord.utils.format_dt(case.created_at, "R")
            items.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(f"**{reason}**\n-# Mod: {moderator} · {when} · Case #{case.id}"),
                    accessory=delete,
                )
            )
        if not warns:
            items.append(discord.ui.TextDisplay("✨ No active warnings, clean record."))
        if ladder := await self._ladder_text(guild_id, user.id):
            items += [discord.ui.Separator(), discord.ui.TextDisplay(ladder)]
        items += [
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"-# Page {page + 1}/{pages} ({len(warns)} warning{'' if len(warns) == 1 else 's'})"),
        ]
        view_items: list[discord.ui.Item] = [
            discord.ui.Container(*items, color=discord.Color.red() if warns else discord.Color.green())
        ]
        if pages > 1:
            buttons = []
            for label, target, disabled in (("◀", page - 1, page == 0), ("▶", page + 1, page >= pages - 1)):
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
                button.callback = self._warn_page_callback(guild_id, user, target)
                buttons.append(button)
            view_items.append(discord.ui.ActionRow(*buttons))
        return discord.ui.DesignerView(*view_items, timeout=900, disable_on_timeout=True)

    def _warn_page_callback(self, guild_id: int, user: discord.abc.User, page: int):
        async def callback(interaction: discord.Interaction):
            if await self._allowed(interaction, "mod.cases.view"):
                view = await self._warnings_view(guild_id, user, page)
                await interaction.response.edit_message(view=view, allowed_mentions=NO_PINGS)

        return callback

    def _warn_delete_callback(self, guild_id: int, user: discord.abc.User, case_id: int, page: int):
        async def callback(interaction: discord.Interaction):
            if not (await self._allowed(interaction, "mod.cases.remove") and await self._outranks(interaction, user.id)):
                return
            removed = await mod_actions.end_case(self.bot, guild_id, case_id, ended_by=interaction.user.id)
            view = await self._warnings_view(guild_id, user, page)
            await interaction.response.edit_message(view=view, allowed_mentions=NO_PINGS)
            if removed is None:
                await interaction.followup.send(self._say(interaction, "already_removed", case_id=case_id), ephemeral=True)
            else:
                text = f"Removed warning #{case_id} for <@{user.id}>."
                await self._log_removal("Warning removed", text, interaction.user.id, user.id)

        return callback

    async def _outranks(self, ctx: discord.ApplicationContext | discord.Interaction, user_id: int) -> bool:
        """Removing cases follows the same rules as acting on the user (helpers can't clear a mod's warns)."""
        target = await resolve_member(ctx.guild, user_id)
        try:
            mod_actions.check_hierarchy(self.bot, ctx.guild, ctx.user, user_id, target, discord_action=False)
        except ModActionError as error:
            await ctx.respond(error.text(reader_language(ctx, self.bot.settings)), ephemeral=True)
            return False
        return True

    async def _remove_case(self, ctx: discord.ApplicationContext, case_id: int, action: str) -> None:
        if not await self._allowed(ctx, "mod.cases.remove"):
            return
        label = "warning" if action == "warn" else action
        case = await mod_cases.get_case(ctx.guild.id, case_id)
        if case is None or case.action != action:
            label_here = self._say(ctx, f"label_{action}")
            return await ctx.respond(self._say(ctx, "not_a", case_id=case_id, label=label_here), ephemeral=True)
        if not await self._outranks(ctx, case.user_id):
            return
        if await mod_actions.end_case(self.bot, ctx.guild.id, case_id, ended_by=ctx.user.id) is None:
            return await ctx.respond(self._say(ctx, "already_removed", case_id=case_id), ephemeral=True)
        text = f"Removed {label} #{case_id} for <@{case.user_id}>."
        await ctx.respond(text, allowed_mentions=NO_PINGS)
        await self._log_removal(f"{label.title()} removed", text, ctx.user.id, case.user_id)

    async def _log_removal(self, title: str, text: str, moderator_id: int, user_id: int) -> None:
        await self._post_mod_log(
            "case_removed", title, f"{text}\nBy <@{moderator_id}>", user_id=moderator_id, data={"target_id": user_id}
        )

    @discord.slash_command(name="delwarn", **described(DESC, "delwarn"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, **described(DESC, "delwarn.case_id"), min_value=1)
    async def delwarn(self, ctx: discord.ApplicationContext, case_id: int):
        await self._remove_case(ctx, case_id, "warn")

    @discord.slash_command(name="delnote", **described(DESC, "delnote"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, **described(DESC, "delnote.case_id"), min_value=1)
    async def delnote(self, ctx: discord.ApplicationContext, case_id: int):
        await self._remove_case(ctx, case_id, "note")

    @discord.slash_command(name="clearwarns", **described(DESC, "clearwarns"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "clearwarns.user"))
    async def clearwarns(self, ctx: discord.ApplicationContext, user: discord.User):
        if not (await self._allowed(ctx, "mod.cases.remove") and await self._outranks(ctx, user.id)):
            return

        async def clear(interaction: discord.Interaction):
            count = await mod_actions.end_user_cases(self.bot, ctx.guild.id, user.id, "warn", ended_by=ctx.user.id)
            text = f"Cleared {count} warning(s) for <@{user.id}>."
            await interaction.followup.send(text, allowed_mentions=NO_PINGS)
            await self._log_removal("Warnings cleared", text, ctx.user.id, user.id)

        language = reader_language(ctx, self.bot.settings)
        prompt = t(TEXT, "clear_prompt", language, user_id=user.id)
        await ctx.respond(
            view=ConfirmView(prompt, t(TEXT, "clear_button", language), clear, language),
            ephemeral=True,
            allowed_mentions=NO_PINGS,
        )

    @discord.slash_command(name="note", **described(DESC, "note"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "note.user"))
    @discord.option("text", str, **described(DESC, "note.text"), max_length=MAX_REASON_LENGTH)
    async def note(self, ctx: discord.ApplicationContext, user: discord.User, text: str):
        if await self._allowed(ctx, "mod.warn"):
            await self._perform(ctx, "note", user.id, text)

    @discord.slash_command(name="notes", **described(DESC, "notes"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "lookup_user"))
    async def notes(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        cases = await mod_cases.list_cases(ctx.guild.id, user_id=user.id, action="note", limit=100)
        notes = [case for case in cases if case.active]
        text = f"### 📝 Notes for {discord.utils.escape_markdown(str(user))}\n{_case_lines(notes[:25]) or 'No notes.'}"
        await ctx.respond(view=reply_card(text[:3800], discord.Color.blurple()), allowed_mentions=NO_PINGS)

    # --- actions -----------------------------------------------------------------------------

    @discord.slash_command(name="mute", **described(DESC, "mute"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "mute.user"))
    @discord.option("duration", str, **described(DESC, "mute.duration"))
    @discord.option("reason", str, **described(DESC, "dm_reason"), max_length=MAX_REASON_LENGTH)
    async def mute(self, ctx: discord.ApplicationContext, user: discord.User, duration: str, reason: str):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        seconds = parse_duration_seconds(duration)
        if not seconds:
            return await ctx.respond(self._say(ctx, "bad_duration"), ephemeral=True)
        await self._perform(ctx, "timeout", user.id, reason, duration_seconds=min(seconds, MAX_TIMEOUT_SECONDS))

    @discord.slash_command(name="unmute", **described(DESC, "unmute"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "unmute.user"))
    @discord.option("reason", str, **described(DESC, "optional"), max_length=MAX_REASON_LENGTH, required=False)
    async def unmute(self, ctx: discord.ApplicationContext, user: discord.User, reason: str = ""):
        if await self._allowed(ctx, "mod.timeout"):
            await self._perform(ctx, "untimeout", user.id, reason or "")

    @discord.slash_command(name="kick", **described(DESC, "kick"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "kick.user"))
    @discord.option("reason", str, **described(DESC, "dm_reason"), max_length=MAX_REASON_LENGTH)
    async def kick(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.kick"):
            await self._perform(ctx, "kick", user.id, reason)

    @discord.slash_command(name="ban", **described(DESC, "ban"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "ban.user"))
    @discord.option("reason", str, **described(DESC, "dm_reason"), max_length=MAX_REASON_LENGTH)
    @discord.option("duration", str, **described(DESC, "ban.duration"), required=False)
    @discord.option(
        "delete_messages",
        str,
        **described(DESC, "ban.delete_messages"),
        choices=list(BAN_DELETE_SECONDS),
        required=False,
    )
    async def ban(
        self,
        ctx: discord.ApplicationContext,
        user: discord.User,
        reason: str,
        duration: str = None,
        delete_messages: str = "7d",
    ):
        if not await self._allowed(ctx, "mod.ban"):
            return
        seconds = parse_duration_seconds(duration) if duration else None
        if duration and not seconds:
            return await ctx.respond(self._say(ctx, "bad_ban_duration"), ephemeral=True)
        await self._perform(
            ctx,
            "ban",
            user.id,
            reason,
            duration_seconds=seconds,
            delete_message_seconds=BAN_DELETE_SECONDS[delete_messages],
        )

    @discord.slash_command(name="softban", **described(DESC, "softban"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "softban.user"))
    @discord.option("reason", str, **described(DESC, "dm_reason"), max_length=MAX_REASON_LENGTH)
    async def softban(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.ban"):
            await self._perform(ctx, "softban", user.id, reason)

    @discord.slash_command(name="unban", **described(DESC, "unban"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user_id", str, **described(DESC, "unban.user_id"))
    @discord.option("reason", str, **described(DESC, "why"), max_length=MAX_REASON_LENGTH)
    async def unban(self, ctx: discord.ApplicationContext, user_id: str, reason: str):
        if not await self._allowed(ctx, "mod.ban"):
            return
        user_id = user_id.strip().strip("<@!>")
        if not re.fullmatch(r"[0-9]{15,20}", user_id):
            return await ctx.respond(self._say(ctx, "bad_user_id"), ephemeral=True)
        await self._perform(ctx, "unban", int(user_id), reason)

    @discord.slash_command(name="purge", **described(DESC, "purge"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("amount", int, **described(DESC, "purge.amount"), min_value=1, max_value=500)
    @discord.option("user", discord.User, **described(DESC, "purge.user"), required=False)
    @discord.option("contains", str, **described(DESC, "purge.contains"), required=False)
    @discord.option("kind", str, **described(DESC, "purge.kind"), choices=list(PURGE_KINDS), required=False)
    async def purge(
        self,
        ctx: discord.ApplicationContext,
        amount: int,
        user: discord.User = None,
        contains: str = None,
        kind: str = None,
    ):
        if not await self._allowed(ctx, "mod.channels"):
            return
        def filters_in(language: str) -> str:
            filters = [
                t(TEXT, "purge_from", language, user_id=user.id) if user else "",
                t(TEXT, "purge_containing", language, text=contains) if contains else "",
                kind or "",
            ]
            return ", ".join(f for f in filters if f)

        filter_text = filters_in("en")  # the mod log's
        language = reader_language(ctx, self.bot.settings)

        async def run(interaction: discord.Interaction):
            try:
                deleted = await ctx.channel.purge(
                    limit=amount,
                    check=purge_check(user_id=user.id if user else None, contains=contains, kind=kind),
                    after=discord.utils.utcnow() - timedelta(days=14),  # older ones can't be bulk-deleted
                    reason=f"/purge by {ctx.user.name}",
                )
            except discord.HTTPException:
                return await interaction.followup.send(t(TEXT, "purge_refused", language), ephemeral=True)
            await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s) from the last 14 days.")
            if not deleted:
                return
            text = f"**Channel** {ctx.channel.mention}　**Moderator** {ctx.user.mention}　**Deleted** {len(deleted)}"
            if filter_text:
                text += f"\n**Filters** {filter_text[:1000]}"
            await self._post_mod_log("purge", "🧹 Purge", text, user_id=ctx.user.id, data={"channel_id": ctx.channel.id})

        prompt = (
            t(TEXT, "purge_some", language, amount=amount, filters=filters_in(language))
            if filter_text
            else t(TEXT, "purge_all", language, amount=amount)
        )
        view = ConfirmView(prompt, t(TEXT, "purge_button", language), run, language)
        await ctx.respond(view=view, ephemeral=True, allowed_mentions=NO_PINGS)

    # --- cases -------------------------------------------------------------------------------

    @discord.slash_command(name="case", **described(DESC, "case"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, **described(DESC, "case.case_id"), min_value=1)
    async def case_show(self, ctx: discord.ApplicationContext, case_id: int):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        case = await mod_cases.get_case(ctx.guild.id, case_id)
        if case is None:
            return await ctx.respond(self._say(ctx, "unknown_case"), ephemeral=True)
        await ctx.respond(view=await mod_actions.render_case_card(self.bot, case), allowed_mentions=NO_PINGS)

    @discord.slash_command(name="reason", **described(DESC, "reason"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, **described(DESC, "reason.case_id"), min_value=1)
    @discord.option("reason", str, **described(DESC, "reason.reason"), max_length=MAX_REASON_LENGTH)
    async def case_reason(self, ctx: discord.ApplicationContext, case_id: int, reason: str):
        if not await self._allowed(ctx, "mod.cases.edit"):
            return
        case = await mod_cases.get_case(ctx.guild.id, case_id)
        if case is None:
            return await ctx.respond(self._say(ctx, "no_case", case_id=case_id), ephemeral=True)
        if not await self._outranks(ctx, case.user_id):
            return
        await mod_cases.update_reason(ctx.guild.id, case_id, reason.strip())
        if updated := await mod_cases.get_case(ctx.guild.id, case_id):
            await mod_actions.refresh_case_card(self.bot, updated)  # the mod-log card shows the new reason too
        await ctx.respond(
            f"Case #{case_id} ({case.action} on <@{case.user_id}>) reason changed:\n"
            f"~~{case.reason or 'No reason'}~~ → {reason.strip()}",
            allowed_mentions=NO_PINGS,
        )

    @discord.slash_command(name="modlogs", **described(DESC, "modlogs"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "lookup_user"))
    async def modlogs(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        cases = await mod_cases.list_cases(ctx.guild.id, user_id=user.id, limit=25)
        text = f"### 📜 Mod logs for {discord.utils.escape_markdown(str(user))}\n{_case_lines(cases) or 'No cases.'}"
        footer = "Struck-through cases are inactive (removed, cleared or expired)."
        await ctx.respond(view=reply_card(text[:3800], discord.Color.blurple(), footer), allowed_mentions=NO_PINGS)

    @discord.slash_command(name="modstats", **described(DESC, "modstats"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("days", int, **described(DESC, "modstats.days"), min_value=1, max_value=3650)
    async def modstats(self, ctx: discord.ApplicationContext, days: int = 30):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        since = discord.utils.utcnow() - timedelta(days=days)
        per_moderator: dict[int, Counter] = defaultdict(Counter)
        for moderator_id, action, count in await mod_cases.moderator_stats(ctx.guild.id, since):
            per_moderator[moderator_id][action] += count
        ranked = sorted(per_moderator.items(), key=lambda item: item[1].total(), reverse=True)[:25]
        lines = [
            f"<@{moderator_id}> **{counts.total()}** · " + ", ".join(f"{action} {n}" for action, n in counts.most_common())
            for moderator_id, counts in ranked
        ]
        text = f"### 📊 Moderator stats, last {days} days\n" + ("\n".join(lines) or "No moderation in that period.")
        await ctx.respond(view=reply_card(text[:3800], discord.Color.blurple()), allowed_mentions=NO_PINGS)

    # --- channels ----------------------------------------------------------------------------

    @discord.slash_command(name="lock", **described(DESC, "lock"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("channel", discord.TextChannel, **described(DESC, "this_channel"), required=False)
    @discord.option("reason", str, **described(DESC, "optional"), max_length=MAX_REASON_LENGTH, required=False)
    async def lock(self, ctx: discord.ApplicationContext, channel: discord.TextChannel = None, reason: str = ""):
        if not await self._allowed(ctx, "mod.channels"):
            return
        channel = channel or ctx.channel
        if isinstance(channel, discord.Thread):
            return await ctx.respond(self._say(ctx, "thread_lock"), ephemeral=True)
        try:
            await mod_actions.lock_channel(
                channel, moderator_id=ctx.user.id, reason=f"{reason or 'No reason given'} (via command by {ctx.user.name})"
            )
        except discord.HTTPException:
            return await ctx.respond(self._say(ctx, "channel_refused"), ephemeral=True)
        await ctx.respond(f"🔒 Locked {channel.mention}.")

    @discord.slash_command(name="unlock", **described(DESC, "unlock"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("channel", discord.TextChannel, **described(DESC, "this_channel"), required=False)
    async def unlock(self, ctx: discord.ApplicationContext, channel: discord.TextChannel = None):
        if not await self._allowed(ctx, "mod.channels"):
            return
        channel = channel or ctx.channel
        if isinstance(channel, discord.Thread):
            return await ctx.respond(self._say(ctx, "thread_unlock"), ephemeral=True)
        try:
            await mod_actions.unlock_channel(channel, reason=f"Unlocked (via command by {ctx.user.name})")
        except discord.HTTPException:
            return await ctx.respond(self._say(ctx, "channel_refused"), ephemeral=True)
        await ctx.respond(f"🔓 Unlocked {channel.mention}.")

    @lockdown_group.command(name="start", **described(DESC, "lockdown.start"))
    @discord.option("reason", str, **described(DESC, "why"), max_length=MAX_REASON_LENGTH)
    async def lockdown_start(self, ctx: discord.ApplicationContext, reason: str):
        if not await self._allowed(ctx, "mod.channels"):
            return

        async def run(interaction: discord.Interaction):
            locked, failed = await mod_actions.lockdown(
                ctx.guild, self.bot.settings, moderator_id=ctx.user.id, reason=f"{reason} (via command by {ctx.user.name})"
            )
            await interaction.followup.send(
                f"🔒 Lockdown: locked {locked} channel(s)" + (f", {failed} failed." if failed else ".")
            )

        language = reader_language(ctx, self.bot.settings)
        prompt, label = t(TEXT, "lockdown_prompt", language), t(TEXT, "lockdown_button", language)
        await ctx.respond(view=ConfirmView(prompt, label, run, language), ephemeral=True)

    @lockdown_group.command(name="end", **described(DESC, "lockdown.end"))
    async def lockdown_end(self, ctx: discord.ApplicationContext):
        if not await self._allowed(ctx, "mod.channels"):
            return
        await ctx.defer()
        unlocked, failed = await mod_actions.end_lockdown(
            ctx.guild, reason=f"Lockdown ended (via command by {ctx.user.name})"
        )
        await ctx.respond(f"🔓 Lockdown over: unlocked {unlocked} channel(s)" + (f", {failed} failed." if failed else "."))

    @discord.slash_command(name="slowmode", **described(DESC, "slowmode"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("seconds", int, **described(DESC, "slowmode.seconds"), min_value=0, max_value=21600)
    @discord.option("channel", discord.TextChannel, **described(DESC, "this_channel"), required=False)
    async def slowmode(self, ctx: discord.ApplicationContext, seconds: int, channel: discord.TextChannel = None):
        if not await self._allowed(ctx, "mod.channels"):
            return
        channel = channel or ctx.channel
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"/slowmode by {ctx.user.name}")
        except discord.HTTPException:
            return await ctx.respond(self._say(ctx, "slowmode_refused"), ephemeral=True)
        await ctx.respond(
            f"🐢 Slowmode in {channel.mention} is now {format_duration(seconds) if seconds else 'off'}."
        )

    # --- members -----------------------------------------------------------------------------

    @discord.slash_command(name="whois", **described(DESC, "whois"))
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, **described(DESC, "lookup_user"))
    async def whois(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        await ctx.defer()  # a profile fetch, a ban lookup and a few DB reads can outlast 3s
        await ctx.respond(view=await self._whois_view(ctx.guild, user, ctx.user), allowed_mentions=NO_PINGS)

    async def _whois_view(
        self, guild: discord.Guild, user: discord.abc.User, viewer: discord.Member
    ) -> discord.ui.DesignerView:
        settings = self.bot.settings
        now = discord.utils.utcnow()
        member = await resolve_member(guild, user.id)
        try:
            profile = await self.bot.fetch_user(user.id)  # banner and accent colour only come with a fetch
        except discord.HTTPException:
            profile = user
        ban = None
        if member is None:
            try:
                ban = await guild.fetch_ban(discord.Object(user.id))
            except discord.HTTPException:
                pass
        cases = await mod_cases.list_cases(guild.id, user_id=user.id, limit=1000)
        steps = parse_ladder(settings.moderation_warn_ladder)
        warn_counts = await self._warn_counts(guild.id, user.id, steps) if steps else {}
        activity = await _quietly(member_activity.get_member_activity(guild.id, user.id))
        open_tickets = await _quietly(ticket_store.get_open_tickets_by_owner(user.id))
        ticket_count = await _quietly(ticket_store.count_tickets_by_owner(guild.id, user.id))
        patreon = (
            await _quietly(patreon_grants.get_patreon_link(user.id))
            if tier_for(viewer, settings) >= PERMISSIONS["patreon.view"]
            else None
        )

        who = member or user
        head = f"## {discord.utils.escape_markdown(who.display_name)}\n{user.mention} · `@{user.name}` · `{user.id}`"
        if badges := [flag.name.replace("_", " ").title() for flag in user.public_flags.all()]:
            head += "\n-# 🏅 " + " · ".join(badges)
        if flags := _whois_flags(user, member, now):
            head += "\n" + "\n".join(f"> {flag}" for flag in flags)

        profile_lines = [
            "### 👤 Profile",
            f"**Created** {discord.utils.format_dt(user.created_at, 'D')} · {discord.utils.format_dt(user.created_at, 'R')}",
        ]
        if member is not None:
            if member.joined_at:
                profile_lines.append(
                    f"**Joined** {discord.utils.format_dt(member.joined_at, 'D')} · {discord.utils.format_dt(member.joined_at, 'R')}"
                )
            if member.premium_since:
                profile_lines.append(f"**Boosting** since {discord.utils.format_dt(member.premium_since, 'R')}")
            if tier := tier_for(member, settings):
                profile_lines.append(f"**Staff** {tier.name.title()}")
            if member.voice and member.voice.channel:
                profile_lines.append(f"**Voice** {member.voice.channel.mention}")
            roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
            shown = " ".join(roles) or "None"
            if len(shown) > 1000:
                shown = shown[:1000].rsplit(" ", 1)[0] + " …"
            profile_lines.append(f"**Roles ({len(roles)})** {shown}")
            permissions = member.guild_permissions
            granted = ["Administrator"] if permissions.administrator else [
                label for name, label in KEY_PERMISSIONS if getattr(permissions, name)
            ]
            if granted:
                profile_lines.append("**Key permissions** " + ", ".join(granted))
        else:
            profile_lines.append("**Not in the server**" + (" (banned)" if ban else ""))
        if activity and activity.xp:
            profile_lines.append(f"**Power level** {activity.level} · {activity.xp:,} XP")

        if ban is not None:
            status = f"🔨 **Banned**: {discord.utils.escape_markdown(trim(ban.reason or 'no reason', 200))}"
        elif member is not None and member.timed_out:
            status = f"🔇 **Timed out**, ends {discord.utils.format_dt(member.communication_disabled_until, 'R')}"
        elif member is None:
            status = "🚪 Not in the server"
        else:
            status = "✅ No active punishment"
        counts = Counter(case.action for case in cases)
        active_warns = sum(case.active and case.action == "warn" for case in cases)
        active_notes = sum(case.active and case.action == "note" for case in cases)
        mod_lines = ["### 🛡️ Moderation", f"**Status** {status}"]
        if cases:
            plural = "" if len(cases) == 1 else "s"
            breakdown = " · ".join(f"{action} {n}" for action, n in counts.most_common())
            mod_lines.append(f"**Record** {len(cases)} case{plural}: {breakdown}")
            mod_lines.append(f"**Active** {active_warns} warn{'' if active_warns == 1 else 's'} · {active_notes} note{'' if active_notes == 1 else 's'}")
        else:
            mod_lines.append("**Record** ✨ Clean, no cases")
        if steps:
            upcoming = pick_step(steps, {window: count + 1 for window, count in warn_counts.items()})
            mod_lines.append(f"**Next warn** → {f'**{_step_text(upcoming)}**' if upcoming else 'no automatic action'}")
        if cases:
            mod_lines.append("**Latest cases**\n" + _case_lines(cases[:3]))

        support_lines = []
        if open_tickets:
            support_lines.append("**Open ticket** " + " ".join(f"<#{ticket.channel_id}>" for ticket in open_tickets))
        if ticket_count:
            support_lines.append(f"**Tickets** {ticket_count} opened")
        if patreon is not None:
            state = "active" if patreon.entitlement_active else "inactive"
            support_lines.append(f"**Patreon** linked, {state}" + (f" ({patreon.patron_status})" if patreon.patron_status else ""))
        if support_lines:
            support_lines.insert(0, "### 🎫 Support")

        if ban is not None:
            color = discord.Color.red()
        elif member is not None and member.timed_out:
            color = discord.Color.orange()
        elif active_warns:
            color = discord.Color.gold()
        elif member is not None and member.color.value:
            color = member.color
        else:
            color = profile.accent_color or discord.Color.blurple()

        head, *blocks = fit([head, "\n".join(profile_lines), "\n".join(mod_lines), "\n".join(support_lines)])
        items: list[discord.ui.Item] = []
        if profile.banner:
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(profile.banner.url)))
        items.append(v2_head(head, user.display_avatar.url))
        for text in blocks:
            items += [discord.ui.Separator(), discord.ui.TextDisplay(text)]
        buttons = []
        for label, emoji, command in (("Warnings", "⚠️", self.warnings), ("Mod logs", "📜", self.modlogs), ("Notes", "📝", self.notes)):
            button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
            button.callback = self._whois_callback(command, user)
            buttons.append(button)
        buttons.append(discord.ui.Button(label="Avatar", style=discord.ButtonStyle.link, url=user.display_avatar.url))
        items.append(discord.ui.ActionRow(*buttons))
        return discord.ui.DesignerView(discord.ui.Container(*items, color=color), timeout=900, disable_on_timeout=True)

    def _whois_callback(self, command, user: discord.abc.User):
        """The whois shortcuts run the same command, with the same gate, as typing it."""

        async def callback(interaction: discord.Interaction):
            await command.callback(self, interaction, user)

        return callback

    # --- context menus -----------------------------------------------------------------------

    @discord.user_command(name="Mod logs")
    @discord.default_permissions(moderate_members=True)
    async def modlogs_menu(self, ctx: discord.ApplicationContext, user: discord.User):
        await self.modlogs.callback(self, ctx, user)

    @discord.user_command(name="Warn")
    @discord.default_permissions(moderate_members=True)
    async def warn_menu(self, ctx: discord.ApplicationContext, user: discord.User):
        if await self._allowed(ctx, "mod.warn"):
            language = reader_language(ctx, self.bot.settings)
            await ctx.send_modal(
                ReasonModal(
                    t(TEXT, "warn_title", language, name=user.name),
                    lambda interaction, reason: self._perform(interaction, "warn", user.id, reason),
                    language,
                )
            )

    @discord.message_command(name="Delete & warn")
    @discord.default_permissions(moderate_members=True)
    async def delete_warn_menu(self, ctx: discord.ApplicationContext, message: discord.Message):
        if not await self._allowed(ctx, "mod.warn"):
            return
        excerpt = (message.content or "[no text]").replace("\n", " ")
        excerpt = excerpt if len(excerpt) <= 80 else excerpt[:79] + "…"

        async def submit(interaction: discord.Interaction, reason: str):
            # Warn first: if the hierarchy check refuses, the message stays.
            if await self._perform(interaction, "warn", message.author.id, f'{reason} (message: "{excerpt}")') is None:
                return
            try:
                await message.delete(reason=f"Delete & warn by {interaction.user.name}")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                await interaction.respond(self._say(interaction, "warned_not_deleted"), ephemeral=True)

        language = reader_language(ctx, self.bot.settings)
        await ctx.send_modal(ReasonModal(t(TEXT, "delete_warn_title", language), submit, language))

    @discord.message_command(name="Mark as scam image")
    @discord.default_permissions(moderate_members=True)
    async def mark_scam_image_menu(self, ctx: discord.ApplicationContext, message: discord.Message):
        if not await self._allowed(ctx, "mod.tools"):
            return
        await ctx.defer()
        hashes = [
            value
            for value in await asyncio.gather(
                *(scam_images.hash_attachment(a) for a in message.attachments if scam_images.is_hashable(a))
            )
            if value is not None
        ]
        if not hashes:
            return await self._private_error(ctx, self._say(ctx, "no_images"))
        try:
            hash_ids = [
                await scam_images.add(value, source="manual", added_by=ctx.user.id, note=f"message {message.id} by {message.author}")
                for value in hashes
            ]
        except Exception:
            log.exception("Couldn't save scam images from message %s", message.id)
            return await self._private_error(ctx, self._say(ctx, "db_down"))
        deleted = "deleted the message"
        try:
            await message.delete(reason=f"Marked as a scam image by {ctx.user.name}")
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("Couldn't delete message %s after marking it as a scam image", message.id, exc_info=True)
            deleted = "couldn't delete the message"
        ids = ", ".join(f"#{hash_id}" for hash_id in hash_ids)
        await ctx.respond(f"🛡️ Added scam image {ids} and {deleted}.")

    # --- scam images -------------------------------------------------------------------------

    # --- tempban expiry ----------------------------------------------------------------------

    async def on_startup(self) -> None:
        if not self.expire_tempbans.is_running():
            self.expire_tempbans.start()

    async def on_shutdown(self) -> None:
        self.expire_tempbans.cancel()

    @tasks.loop(minutes=1)
    async def expire_tempbans(self):
        try:
            due = await mod_cases.due_expirations(discord.utils.utcnow())
        except Exception:
            log.exception("Couldn't load due tempbans")
            return
        for case in due:
            guild = self.bot.get_guild(case.guild_id)
            if guild is None:
                continue
            try:
                if case.action in ("timeout", "warn"):  # Discord already lifted a timeout; the card flips to "expired"
                    await mod_actions.end_case(self.bot, case.guild_id, case.id, ended_by=None, note="expired")
                    continue
                if case.action != "ban":
                    continue
                try:
                    await mod_actions.perform(
                        self.bot,
                        guild,
                        action="unban",
                        target_id=case.user_id,
                        moderator=None,
                        reason=f"Tempban expired (case #{case.id})",
                        source="tempban",
                        notify=False,
                    )
                except ModActionError as error:
                    if error.status != 404:
                        raise
                    await mod_actions.end_case(self.bot, case.guild_id, case.id, ended_by=None)  # unbanned by hand
            except Exception:
                log.warning("Couldn't expire case #%s", case.id, exc_info=True)

    @expire_tempbans.before_loop
    async def _before_expire_tempbans(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(ModCommandsCog(bot))
