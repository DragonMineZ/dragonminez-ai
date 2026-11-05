"""The clickable side of moderation: quick-action buttons on staff alerts, ban appeals and member message
reports. The views in ui/mod_views.py have no callbacks; their custom ids carry the target, so the
on_interaction listener below routes clicks by prefix and they keep working after a restart."""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

import discord
from discord.ext import commands

from bulmaai.config import set_setting_override
from bulmaai.services import automod_hits, ban_appeals, joiner_alerts, mod_actions, mod_alert_cards, mod_cases
from bulmaai.services.ai_guard import defang
from bulmaai.ui.mod_cards import (
    ALERT,
    ALERT_HANDLED_ID,
    ALERT_SUMMARY_ID,
    alert_card,
    alert_container,
    clone_buttons,
    collapsed_alert,
    detail_dicts,
    expanded_alert,
    handled_lines,
    quote_block,
    summary_text,
    trim_lines,
    user_line,
)
from bulmaai.ui.mod_views import (
    APPEAL,
    APPEAL_REVIEW,
    QUICK,
    QUICK_ACTIONS,
    TUNE,
    allowlist_button,
    appeal_review_buttons,
    parse_custom_id,
    quick_action_buttons,
)
from bulmaai.web.core import PERMISSIONS, tier_for
from bulmaai.ui.v2 import card, unbusy, while_busy
from bulmaai.utils.i18n import reader_language, t, table, user_language

log = logging.getLogger(__name__)

QUICK_PERMISSIONS = {
    "delete": "mod.warn",
    "learn": "mod.tools",
    "warn": "mod.warn",
    "dismiss": "mod.warn",
    "falsepos": "mod.tools",
    "timeout": "mod.timeout",
    "untimeout": "mod.timeout",
    "kick": "mod.kick",
    "ban": "mod.ban",
}
ASK_REASON = {"warn", "kick", "ban"}  # the reason modal doubles as a misclick guard
CONFIRM_HIT_ACTIONS = {"warn", "timeout", "kick", "ban"}  # punitive clicks confirm an automod hit, without learning
APPEAL_CHECK_SECONDS = 2
QUICK_TIMEOUT_SECONDS = 86400
REPORT_ACTIONS = ("delete", "warn", "timeout", "ban", "dismiss")
REPORT_COOLDOWN_SECONDS = 60
NO_MENTIONS = discord.AllowedMentions.none()

OnSubmit = Callable[[discord.Interaction, str], Awaitable[None]]

# Ephemeral replies, forms and DMs are in the reader's language; the alert cards' handled lines stay English.
TEXT = table({
    "en": {
        "failed": "Something went wrong, the bot logs have the details.",
        "main_only": "That only works on the DragonMineZ server.",
        "tier_denied": "Your staff tier can't do that.",
        "details_gone": "The saved details for this alert are gone.",
        "action_failed": "Something went wrong, the action may not have gone through.",
        "quick_title": "{label} user {user_id}",
        "reason": "Reason",
        "done": "Done: {label}.",
        "done_case": "Done: {label} (case #{case_id}).",
        "not_dmed": " They couldn't be DMed.",
        "ladder_hit": "\nThat hit the warn ladder: automatic {action}{case}.",
        "ladder_skipped": "\nWarn ladder: {skipped}.",
        "learned": "Deleted the message and learned {count} images.",
        "learned_one": "Deleted the message and learned 1 image.",
        "learned_none": "Deleted the message; nothing was learned.",
        "no_hit": "No automod record was found for this alert; dismissed instead.",
        "falsepos": "Marked as a false positive ({summary}).",
        "nothing_undone": "nothing needed to be undone",
        "allowlist_prompt": "Also stop flagging these domains?",
        "allowlist_admin": "An admin can allowlist these domains in Settings.",
        "channel_gone": "That channel is gone or the bot can't see it.",
        "delete_forbidden": "The bot isn't allowed to delete messages in that channel.",
        "discord_error": "Discord returned an error, try again.",
        "no_domains": "That automod hit has no domains to allowlist any more.",
        "allowlisted": "✅ Allowlisted: {domains}.",
        "not_banned": "You're no longer banned from **{guild}**.",
        "ban_check_failed": "Couldn't check your ban right now, try again later.",
        "appeals_down": "Appeals are unavailable right now, try again later.",
        "already_appealed": "You already appealed this ban; staff review each ban's appeal once.",
        "appeal_site": "Appeals are made on the appeals website now: {url}",
        "appeals_closed": "Ban appeals are closed right now.",
        "server_gone": "I can't reach that server any more.",
        "appeal_title": "Appeal your ban from {guild}",
        "appeal_label": "Why should your ban be lifted?",
        "appeal_save_failed": "Couldn't save your appeal, try again later.",
        "appeal_sent": "Your appeal was sent to the **{guild}** staff.",
        "appeal_failed": "Something went wrong, the appeal may not have been answered.",
        "deny_title": "Deny appeal",
        "deny_label": "Reason (optional, sent to the user)",
        "appeal_accepted_dm": "Your ban appeal for **{guild}** was accepted, you can rejoin the server.",
        "appeal_denied_dm": "Your ban appeal for **{guild}** was denied.",
        "appeal_denied_reason": "\nReason: {reason}",
        "accepted": "Accepted",
        "denied": "Denied",
        "report_main_only": "Reports only work on the DragonMineZ server.",
        "report_bot": "You can't report a bot's message.",
        "report_own": "You can't report your own message.",
        "report_wait": "You just sent a report, try again in {wait}s.",
        "report_title": "Report message",
        "report_label": "What's wrong with it? (optional)",
        "report_failed": "Couldn't reach the staff right now, try again later.",
        "report_sent": "Thanks, staff will take a look.",
        "quick_delete": "Delete message",
        "quick_learn": "Delete & learn",
        "quick_warn": "Warn",
        "quick_timeout": "Timeout 24h",
        "quick_untimeout": "Remove timeout",
        "quick_kick": "Kick",
        "quick_ban": "Ban",
        "quick_dismiss": "Dismiss",
        "quick_falsepos": "False positive",
    },
    "es": {
        "failed": "Algo salió mal; los logs del bot tienen los detalles.",
        "main_only": "Eso solo funciona en el servidor de DragonMineZ.",
        "tier_denied": "Tu nivel de staff no puede hacer eso.",
        "details_gone": "Los detalles guardados de esta alerta ya no están.",
        "action_failed": "Algo salió mal, puede que la acción no se haya aplicado.",
        "quick_title": "{label}: usuario {user_id}",
        "reason": "Motivo",
        "done": "Listo: {label}.",
        "done_case": "Listo: {label} (caso #{case_id}).",
        "not_dmed": " No se le pudo enviar MD.",
        "ladder_hit": "\nEso activó la escalera de advertencias: {action} automático{case}.",
        "ladder_skipped": "\nEscalera de advertencias: {skipped}.",
        "learned": "Borré el mensaje y aprendí {count} imágenes.",
        "learned_one": "Borré el mensaje y aprendí 1 imagen.",
        "learned_none": "Borré el mensaje; no se aprendió nada.",
        "no_hit": "No encontré un registro de automod para esta alerta; la descarté.",
        "falsepos": "Marcado como falso positivo ({summary}).",
        "nothing_undone": "no hubo nada que deshacer",
        "allowlist_prompt": "¿Dejar también de marcar estos dominios?",
        "allowlist_admin": "Un admin puede permitir estos dominios en la Configuración.",
        "channel_gone": "Ese canal ya no existe o el bot no lo ve.",
        "delete_forbidden": "El bot no tiene permiso para borrar mensajes en ese canal.",
        "discord_error": "Discord devolvió un error, inténtalo de nuevo.",
        "no_domains": "Ese registro de automod ya no tiene dominios que permitir.",
        "allowlisted": "✅ Permitidos: {domains}.",
        "not_banned": "Ya no estás baneado de **{guild}**.",
        "ban_check_failed": "No pude revisar tu baneo ahora, inténtalo más tarde.",
        "appeals_down": "Las apelaciones no están disponibles ahora, inténtalo más tarde.",
        "already_appealed": "Ya apelaste este baneo; el staff revisa la apelación de cada baneo una sola vez.",
        "appeal_site": "Las apelaciones ahora se hacen en la web de apelaciones: {url}",
        "appeals_closed": "Las apelaciones de baneo están cerradas ahora.",
        "server_gone": "Ya no puedo acceder a ese servidor.",
        "appeal_title": "Apela tu baneo de {guild}",
        "appeal_label": "¿Por qué deberían quitarte el baneo?",
        "appeal_save_failed": "No pude guardar tu apelación, inténtalo más tarde.",
        "appeal_sent": "Tu apelación se envió al staff de **{guild}**.",
        "appeal_failed": "Algo salió mal, puede que la apelación no se haya respondido.",
        "deny_title": "Rechazar apelación",
        "deny_label": "Motivo (opcional, se le envía al usuario)",
        "appeal_accepted_dm": "Tu apelación de baneo en **{guild}** fue aceptada, puedes volver a unirte al servidor.",
        "appeal_denied_dm": "Tu apelación de baneo en **{guild}** fue rechazada.",
        "appeal_denied_reason": "\nMotivo: {reason}",
        "accepted": "Aceptada",
        "denied": "Rechazada",
        "report_main_only": "Los reportes solo funcionan en el servidor de DragonMineZ.",
        "report_bot": "No puedes reportar el mensaje de un bot.",
        "report_own": "No puedes reportar tu propio mensaje.",
        "report_wait": "Acabas de enviar un reporte, inténtalo de nuevo en {wait}s.",
        "report_title": "Reportar mensaje",
        "report_label": "¿Qué tiene de malo? (opcional)",
        "report_failed": "No pude contactar al staff ahora, inténtalo más tarde.",
        "report_sent": "Gracias, el staff lo revisará.",
        "quick_delete": "Borrar mensaje",
        "quick_learn": "Borrar y aprender",
        "quick_warn": "Advertir",
        "quick_timeout": "Timeout 24h",
        "quick_untimeout": "Quitar timeout",
        "quick_kick": "Expulsar",
        "quick_ban": "Banear",
        "quick_dismiss": "Descartar",
        "quick_falsepos": "Falso positivo",
    },
    "pt": {
        "failed": "Algo deu errado; os logs do bot têm os detalhes.",
        "main_only": "Isso só funciona no servidor do DragonMineZ.",
        "tier_denied": "Seu nível de staff não pode fazer isso.",
        "details_gone": "Os detalhes salvos deste alerta não existem mais.",
        "action_failed": "Algo deu errado, a ação pode não ter sido aplicada.",
        "quick_title": "{label}: usuário {user_id}",
        "reason": "Motivo",
        "done": "Feito: {label}.",
        "done_case": "Feito: {label} (caso #{case_id}).",
        "not_dmed": " Não foi possível enviar DM a ele.",
        "ladder_hit": "\nIsso ativou a escada de advertências: {action} automático{case}.",
        "ladder_skipped": "\nEscada de advertências: {skipped}.",
        "learned": "Apaguei a mensagem e aprendi {count} imagens.",
        "learned_one": "Apaguei a mensagem e aprendi 1 imagem.",
        "learned_none": "Apaguei a mensagem; nada foi aprendido.",
        "no_hit": "Nenhum registro de automod foi encontrado para este alerta; descartei.",
        "falsepos": "Marcado como falso positivo ({summary}).",
        "nothing_undone": "não havia nada para desfazer",
        "allowlist_prompt": "Parar também de marcar estes domínios?",
        "allowlist_admin": "Um admin pode liberar estes domínios nas Configurações.",
        "channel_gone": "Esse canal não existe mais ou o bot não consegue vê-lo.",
        "delete_forbidden": "O bot não tem permissão para apagar mensagens nesse canal.",
        "discord_error": "O Discord retornou um erro, tente novamente.",
        "no_domains": "Esse registro de automod não tem mais domínios para liberar.",
        "allowlisted": "✅ Liberados: {domains}.",
        "not_banned": "Você não está mais banido de **{guild}**.",
        "ban_check_failed": "Não consegui verificar seu banimento agora, tente mais tarde.",
        "appeals_down": "Os recursos estão indisponíveis agora, tente mais tarde.",
        "already_appealed": "Você já recorreu deste banimento; a staff analisa o recurso de cada banimento uma vez.",
        "appeal_site": "Os recursos agora são feitos no site de recursos: {url}",
        "appeals_closed": "Os recursos de banimento estão fechados agora.",
        "server_gone": "Não consigo mais acessar esse servidor.",
        "appeal_title": "Recorra do seu banimento em {guild}",
        "appeal_label": "Por que seu banimento deveria ser retirado?",
        "appeal_save_failed": "Não consegui salvar seu recurso, tente mais tarde.",
        "appeal_sent": "Seu recurso foi enviado à staff de **{guild}**.",
        "appeal_failed": "Algo deu errado, o recurso pode não ter sido respondido.",
        "deny_title": "Negar recurso",
        "deny_label": "Motivo (opcional, enviado ao usuário)",
        "appeal_accepted_dm": "Seu recurso de banimento em **{guild}** foi aceito, você pode voltar ao servidor.",
        "appeal_denied_dm": "Seu recurso de banimento em **{guild}** foi negado.",
        "appeal_denied_reason": "\nMotivo: {reason}",
        "accepted": "Aceito",
        "denied": "Negado",
        "report_main_only": "Denúncias só funcionam no servidor do DragonMineZ.",
        "report_bot": "Você não pode denunciar a mensagem de um bot.",
        "report_own": "Você não pode denunciar sua própria mensagem.",
        "report_wait": "Você acabou de enviar uma denúncia, tente de novo em {wait}s.",
        "report_title": "Denunciar mensagem",
        "report_label": "O que há de errado com ela? (opcional)",
        "report_failed": "Não consegui falar com a staff agora, tente mais tarde.",
        "report_sent": "Obrigado, a staff vai dar uma olhada.",
        "quick_delete": "Apagar mensagem",
        "quick_learn": "Apagar e aprender",
        "quick_warn": "Advertir",
        "quick_timeout": "Timeout 24h",
        "quick_untimeout": "Remover timeout",
        "quick_kick": "Expulsar",
        "quick_ban": "Banir",
        "quick_dismiss": "Dispensar",
        "quick_falsepos": "Falso positivo",
    },
})


class TextModal(discord.ui.Modal):
    """One long text box; hands the trimmed text to on_submit. Only lives while the user has it open."""

    def __init__(
        self,
        *,
        title: str,
        custom_id: str,
        label: str,
        on_submit: OnSubmit,
        required: bool = True,
        min_length: int | None = None,
        max_length: int = 512,
        value: str | None = None,
    ):
        super().__init__(title=title[:45], custom_id=custom_id)
        self.text = discord.ui.InputText(
            label=label,
            style=discord.InputTextStyle.long,
            required=required,
            min_length=min_length,
            max_length=max_length,
            value=value,
        )
        self.add_item(self.text)
        self.on_submit = on_submit

    async def callback(self, interaction: discord.Interaction):
        await self.on_submit(interaction, (self.text.value or "").strip())

    async def on_error(self, error: Exception, interaction: discord.Interaction) -> None:
        await _fail(interaction, error, self.custom_id)


# --- small helpers -----------------------------------------------------------------------------


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def _fail(interaction: discord.Interaction, error: Exception, custom_id: str | None) -> None:
    log.error(
        "Moderation interaction failed",
        exc_info=error,
        extra={"event": "mod_interaction_failed", "custom_id": custom_id, "user_id": interaction.user.id},
    )
    try:
        await _reply(interaction, t(TEXT, "failed", reader_language(interaction)))
    except discord.HTTPException:
        pass


def _first_text(components) -> str | None:
    for component in components:
        if content := getattr(component, "content", None):
            return content
        if found := _first_text(getattr(component, "components", None) or []):
            return found
    return None


def _alert_summary(message: discord.Message | None) -> str | None:
    """The alert card's first text, flattened and trimmed, to prefill reasons."""
    if message is None:
        return None
    text = (_first_text(getattr(message, "components", None) or []) or "").lstrip("# ")
    return " ".join(text.split())[:200] or None


async def _start(interaction: discord.Interaction) -> None:
    """A button click already answered through while_busy (its card shows Working); a reason modal still has to."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, invisible=False)


async def _put_back(interaction: discord.Interaction) -> None:
    """After an error a button click already explained: its card comes back from Working, untouched."""
    if interaction.type == discord.InteractionType.component:
        await unbusy(interaction)


async def _mark_handled(message: discord.Message | None, line: str, custom_id: str | None = None) -> None:
    """Records line on the alert card and disables the clicked button (every quick button when custom_id is None).
    The card collapses to a one-liner (modcard:show brings the details back); its buttons are rebuilt from the
    message's own components so the rest stay."""
    if message is not None:
        await _mark_card_handled(message, line, custom_id)


async def _mark_card_handled(message: discord.Message, line: str, custom_id: str | None) -> None:
    container = alert_container(discord.ui.DesignerView.from_message(message, timeout=None))
    if container is None:
        return
    lines = trim_lines(handled_lines(container) + [line])
    buttons = clone_buttons(container, quick_disabled=custom_id or "")
    if summary := container.get_item(ALERT_SUMMARY_ID):  # collapsed: stay collapsed
        view = collapsed_alert(summary.content, lines, buttons)
    elif container.get_item(ALERT_HANDLED_ID):  # expanded after "Show details": stay expanded
        view = expanded_alert(detail_dicts(container), lines, buttons)
    else:  # first click: save the details, then collapse
        details = detail_dicts(container)
        try:
            await mod_alert_cards.save(message.id, details)
            view = collapsed_alert(summary_text(details), lines, buttons)
        except Exception:
            log.exception(
                "Couldn't save an alert's details; leaving it expanded",
                extra={"event": "mod_alert_card_save_failed", "message_id": message.id},
            )
            view = expanded_alert(details, lines, buttons, toggle=False)
    try:
        await message.edit(view=view, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException:
        log.warning(
            "Couldn't update a handled moderation message",
            exc_info=True,
            extra={"event": "mod_handled_edit_failed", "message_id": message.id},
        )


def _case_summary(cases: list[mod_cases.ModCase]) -> str:
    lines = [
        f"#{case.id} {case.action} {discord.utils.format_dt(case.created_at, 'd')}"
        + (f": {case.reason[:60]}" if case.reason else "")
        for case in cases[:5]
    ]
    return "\n".join(lines)[:1024] or "None"


def _avatar(user) -> str | None:
    return getattr(getattr(user, "display_avatar", None), "url", None)


def _appeal_card(user: discord.abc.User, ban: discord.guild.BanEntry, text: str, cases, case_id: int | None):
    head = [
        "### 📨 Ban appeal",
        (f"**Case** #{case_id}　" if case_id else "") + f"**Ban reason** {defang(ban.reason or 'No reason given')[:500]}",
        f"**Recent cases**\n{_case_summary(cases)}",
    ]
    # Member-written text in a staff channel: quoted, defanged (no clickable or masked phishing links).
    return alert_card(
        "\n".join(head),
        _avatar(user),
        appeal_review_buttons(user.id),
        color=discord.Color.blurple(),
        snapshot=user_line(user, f"Account created {discord.utils.format_dt(user.created_at, 'R')}"),
        quote=quote_block(text, 1000),
    )


def _report_card(reporter: discord.abc.User, message: discord.Message, reason: str) -> discord.ui.DesignerView:
    head = ["### 🚩 Reported message"]
    if reason:
        head.append(f"**Reason** {discord.utils.escape_markdown(defang(reason))}")
    head.append(f"**Reporter** {reporter.mention}　**Channel** <#{message.channel.id}>")
    if message.attachments:
        head.append("**Attachments** " + ", ".join(f"`{a.filename.replace('`', '')}`" for a in message.attachments[:10]))
    return alert_card(
        "\n".join(head)[:1800],
        _avatar(message.author),
        quick_action_buttons(message.author.id, actions=REPORT_ACTIONS, message=(message.channel.id, message.id)),
        color=discord.Color.orange(),
        snapshot=user_line(message.author),
        quote=quote_block(message.content) or "> *no text*",
    )


# --- the cog -----------------------------------------------------------------------------------


class ModInteractionsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        # ponytail: in-memory per-reporter cooldown; resets on restart, pruned on every report.
        self._last_report: dict[int, float] = {}
        self._working: set[int] = set()  # messages whose button click is still being handled (ui.v2.while_busy)

    @property
    def settings(self):
        return self.bot.settings

    def _say(self, interaction, key: str, **values) -> str:
        """TEXT[key] in the language of whoever clicked, submitted or ran the command."""
        return t(TEXT, key, reader_language(interaction, self.settings), **values)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id")
        parsed = parse_custom_id(custom_id)
        handler = parsed and {
            QUICK: self._quick,
            APPEAL: self._appeal,
            APPEAL_REVIEW: self._review,
            TUNE: self._tune,
            ALERT: self._card,
        }.get(parsed[0])
        if not handler:
            return  # someone else's button, or modraid: (cogs/raid_guard.py)
        try:
            await handler(interaction, parsed[1])
        except Exception as error:
            await _fail(interaction, error, custom_id)

    async def _staff(self, interaction: discord.Interaction, permission: str) -> bool:
        guild = interaction.guild
        if guild is None or guild.id != self.settings.panel_guild_id:
            await _reply(interaction, self._say(interaction, "main_only"))
            return False
        if tier_for(interaction.user, self.settings) < PERMISSIONS[permission]:
            await _reply(interaction, self._say(interaction, "tier_denied"))
            return False
        return True

    async def _dm(self, user_id: int, *keys: str, **values) -> None:
        """TEXT[keys] joined, in the user's own language."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            language = user_language(user, self.settings, client=self.bot)
            await user.send("".join(t(TEXT, key, language, **values) for key in keys))
        except discord.HTTPException:
            pass  # usually they share no server with the bot any more

    # --- alert cards (modcard:<show|hide>) ---

    async def _card(self, interaction: discord.Interaction, parts: list[str]) -> None:
        """Expands a collapsed automod alert from its saved details, or collapses it again."""
        if parts[0] not in ("show", "hide") or not await self._staff(interaction, "mod.warn"):
            return
        message = interaction.message
        container = alert_container(discord.ui.DesignerView.from_message(message, timeout=None))
        if container is None:
            return
        lines, buttons = handled_lines(container), clone_buttons(container)
        if parts[0] == "show":
            details = await mod_alert_cards.load(message.id)
            if not details:
                await _reply(interaction, self._say(interaction, "details_gone"))
                return
            view = expanded_alert(details, lines, buttons)
        else:
            view = collapsed_alert(summary_text(detail_dicts(container)), lines, buttons)
        await interaction.response.edit_message(view=view, allowed_mentions=NO_MENTIONS)

    # --- quick actions (modqa:<action>:<user_id>[:<channel_id>:<message_id>]) ---

    async def _quick(self, interaction: discord.Interaction, parts: list[str]) -> None:
        action, user_id = parts[0], int(parts[1])
        if action not in QUICK_PERMISSIONS or not await self._staff(interaction, QUICK_PERMISSIONS[action]):
            return
        alert = interaction.message
        custom_id = interaction.data["custom_id"]
        if action not in ASK_REASON:
            reason = _alert_summary(alert) or "Quick action from a staff alert"

            async def run() -> None:
                await self._run_quick(interaction, action, user_id, parts[2:], alert, custom_id, reason)

            await while_busy(interaction, self._working, run, failed=self._say(interaction, "action_failed"))
            return

        async def submit(modal_interaction: discord.Interaction, reason: str) -> None:
            message = modal_interaction.message or alert  # the fresher copy when Discord sends one
            await self._run_quick(modal_interaction, action, user_id, [], message, custom_id, reason)

        label = self._say(interaction, f"quick_{action}")
        await interaction.response.send_modal(
            TextModal(
                title=self._say(interaction, "quick_title", label=label, user_id=user_id),
                custom_id=f"{QUICK}-reason:{action}:{user_id}",
                label=self._say(interaction, "reason"),
                value=_alert_summary(alert),
                on_submit=submit,
            )
        )

    async def _run_quick(
        self,
        interaction: discord.Interaction,
        action: str,
        user_id: int,
        extra: list[str],
        alert: discord.Message | None,
        custom_id: str,
        reason: str,
    ) -> None:
        await _start(interaction)
        moderator = interaction.user
        if action == "falsepos":
            await self._false_positive(interaction, alert, moderator)
            return
        if action == "learn":
            await self._delete_and_learn(interaction, extra, alert, moderator)
            return
        label = QUICK_ACTIONS[action][0]
        line = f"{label} by {moderator.mention}"  # on the shared alert card: English
        language = reader_language(interaction, self.settings)
        label_here = t(TEXT, f"quick_{action}", language)
        reply = t(TEXT, "done", language, label=label_here)
        if action == "delete":
            if error := await self._delete(int(extra[0]), int(extra[1]), moderator):
                await interaction.followup.send(t(TEXT, error, language), ephemeral=True)
                return await _put_back(interaction)
        elif action != "dismiss":
            try:
                result = await mod_actions.perform(
                    self.bot,
                    interaction.guild,
                    action=action,
                    target_id=user_id,
                    moderator=moderator,
                    reason=reason,
                    duration_seconds=QUICK_TIMEOUT_SECONDS if action == "timeout" else None,
                    source="alert",
                )
            except mod_actions.ModActionError as error:
                await interaction.followup.send(error.text(language), ephemeral=True)
                return await _put_back(interaction)
            if result.case_id:
                line += f" | case #{result.case_id}"
                reply = t(TEXT, "done_case", language, label=label_here, case_id=result.case_id)
            if result.dm_sent is False:
                reply += t(TEXT, "not_dmed", language)
            if escalation := result.escalation:
                case = f" (#{escalation.case_id})" if escalation.case_id else ""
                line += f" -> automatic {escalation.action}"
                reply += t(TEXT, "ladder_hit", language, action=escalation.action, case=case)
            if result.ladder_skipped:
                line += f" -> {result.ladder_skipped}"
                reply += t(TEXT, "ladder_skipped", language, skipped=result.ladder_skipped)
            if action in CONFIRM_HIT_ACTIONS:
                await self._confirm_hit(alert, moderator.id)  # best-effort; never learns images on its own
        await _mark_handled(alert, line, None if action == "dismiss" else custom_id)
        await self._resolve_joiner_alert(alert, moderator.id)  # best-effort; no-ops on non-joiner alerts
        await interaction.followup.send(reply, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _resolve_joiner_alert(self, alert: discord.Message | None, moderator_id: int) -> None:
        """Marks a flagged-joiner alert (raid_guard) as staff-handled, so its 1h sweep leaves it alone."""
        if alert is None:
            return
        try:
            record = await joiner_alerts.alert_for_message(alert.id)
            if record is not None:
                await joiner_alerts.set_outcome(record.id, joiner_alerts.HANDLED, moderator_id)
        except Exception:
            log.exception(
                "Couldn't resolve a flagged-joiner alert", extra={"event": "mod_joiner_alert_resolve_failed", "message_id": alert.id}
            )

    async def _confirm_hit(self, alert: discord.Message | None, moderator_id: int, *, learn: bool = False) -> int:
        """Best-effort: confirms this alert's automod hit, if any. learn=True (Delete & learn) also teaches its
        images to the scam list; a punitive click never learns on its own. Failures are logged, not raised."""
        if alert is None:
            return 0
        try:
            hit = await automod_hits.hit_for_alert(alert.id)
            if hit is None:
                return 0
            return await automod_hits.mark_confirmed(hit, moderator_id, learn=learn)
        except Exception:
            log.exception(
                "Couldn't record an automod confirmation",
                extra={"event": "mod_confirm_hit_failed", "message_id": alert.id},
            )
            return 0

    async def _delete_and_learn(
        self,
        interaction: discord.Interaction,
        extra: list[str],
        alert: discord.Message | None,
        moderator: discord.Member,
    ) -> None:
        """The "Delete & learn" quick action on image alerts: delete like the plain delete action, then an
        explicit, staff-decided learn of the hit's images (mark_confirmed(..., learn=True))."""
        if error := await self._delete(int(extra[0]), int(extra[1]), moderator):
            await interaction.followup.send(self._say(interaction, error), ephemeral=True)
            return await _put_back(interaction)
        learned = await self._confirm_hit(alert, moderator.id, learn=True)
        plural = "s" if learned != 1 else ""
        line = f"Deleted & learned {learned} image{plural} by {moderator.mention}"
        key = "learned_none" if not learned else "learned_one" if learned == 1 else "learned"
        reply = self._say(interaction, key, count=learned)
        await _mark_handled(alert, line, None)
        await interaction.followup.send(reply, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def _false_positive(
        self, interaction: discord.Interaction, alert: discord.Message | None, moderator: discord.Member
    ) -> None:
        """The "False positive" quick action: undoes the automod hit tied to this alert (or, with no hit on
        record, just dismisses it) and, for link filters, offers an admin the chance to allowlist the domains."""
        hit = None
        if alert is not None:
            try:
                hit = await automod_hits.hit_for_alert(alert.id)
            except Exception:
                log.exception(
                    "Couldn't look up an automod hit",
                    extra={"event": "mod_falsepos_lookup_failed", "message_id": alert.id},
                )
        if hit is None:
            await _mark_handled(alert, f"{QUICK_ACTIONS['dismiss'][0]} by {moderator.mention}", None)
            await interaction.followup.send(
                self._say(interaction, "no_hit"), ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        undone = await automod_hits.mark_false_positive(self.bot, interaction.guild, hit, moderator)
        summary = ", ".join(undone) if undone else "nothing needed to be undone"
        await _mark_handled(alert, f"False positive by {moderator.mention} ({summary})", None)
        summary_here = ", ".join(undone) if undone else self._say(interaction, "nothing_undone")
        await interaction.followup.send(
            self._say(interaction, "falsepos", summary=summary_here), ephemeral=True, allowed_mentions=NO_MENTIONS
        )
        if hit.domains:
            if tier_for(moderator, self.settings) >= PERMISSIONS["settings.edit"]:
                buttons = [allowlist_button(hit.id, hit.domains)]
                prompt = card(self._say(interaction, "allowlist_prompt"), buttons=buttons)
                await interaction.followup.send(view=prompt, ephemeral=True)
            else:
                await interaction.followup.send(self._say(interaction, "allowlist_admin"), ephemeral=True)

    async def _delete(self, channel_id: int, message_id: int, moderator: discord.Member) -> str | None:
        """None when the message is deleted (or already gone), else the TEXT key of what to tell the clicker."""
        channel = await mod_actions.resolve_channel(self.bot, channel_id)
        if channel is None or not hasattr(channel, "get_partial_message"):
            return "channel_gone"
        try:
            await channel.get_partial_message(message_id).delete(reason=f"Staff alert quick action by {moderator.name}")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            return "delete_forbidden"
        except discord.HTTPException:
            log.warning(
                "Quick-action delete failed",
                exc_info=True,
                extra={"event": "mod_quick_delete_failed", "channel_id": channel_id, "message_id": message_id},
            )
            return "discord_error"
        return None

    # --- automod tuning (modtune:allow:<automod_hit_id>, offered after a false positive) ---

    async def _tune(self, interaction: discord.Interaction, parts: list[str]) -> None:
        action, hit_id = parts[0], int(parts[1])
        if action != "allow" or not await self._staff(interaction, "settings.edit"):
            return
        hit = await automod_hits.get_hit(hit_id)
        if hit is None or not hit.domains:
            await _reply(interaction, self._say(interaction, "no_domains"))
            return
        current = list(self.settings.moderation_allowed_domains)
        merged = current + [domain for domain in hit.domains if domain not in current]
        set_setting_override("moderation_allowed_domains", ",".join(merged))
        self.bot.reload_settings()
        log.info(
            "Allowlisted domains from a false-positive automod hit",
            extra={"event": "mod_tune_allowlisted", "hit_id": hit_id, "domains": merged, "user_id": interaction.user.id},
        )
        done = self._say(interaction, "allowlisted", domains=", ".join(hit.domains))
        await interaction.response.edit_message(view=card(done, color=discord.Color.green()))

    # --- appeals (modappeal:<guild_id> in the ban DM, modappeal-review:<accept|deny>:<user_id>) ---

    async def _appeal_check(self, guild: discord.Guild, user: discord.abc.User):
        """(refusal or None, ban entry, the user's recent cases newest first); the refusal is a TEXT key (it takes
        guild=)."""
        try:
            ban = await guild.fetch_ban(user)
        except discord.NotFound:
            return "not_banned", None, []
        except discord.HTTPException:
            extra = {"event": "mod_appeal_ban_fetch_failed", "user_id": user.id}
            log.warning("Couldn't fetch a ban", exc_info=True, extra=extra)
            return "ban_check_failed", None, []
        try:
            cases = await mod_cases.list_cases(guild.id, user_id=user.id, limit=25)
        except Exception:
            extra = {"event": "mod_appeal_cases_failed", "user_id": user.id}
            log.exception("Couldn't load cases for an appeal", extra=extra)
            return "appeals_down", None, []
        latest = next((case for case in cases if case.action in ("ban", "appeal")), None)
        if latest is not None and latest.action == "appeal":
            return "already_appealed", ban, cases
        return None, ban, cases

    async def _appeal(self, interaction: discord.Interaction, parts: list[str]) -> None:
        if (url := ban_appeals.site_url(self.settings)) and int(parts[0]) == getattr(self.settings, "panel_guild_id", None):
            await _reply(interaction, self._say(interaction, "appeal_site", url=url))
            return
        if not self.settings.moderation_appeals_enabled:
            await _reply(interaction, self._say(interaction, "appeals_closed"))
            return
        guild = self.bot.get_guild(int(parts[0]))
        if guild is None:
            await _reply(interaction, self._say(interaction, "server_gone"))
            return
        try:
            # Discord drops the click unless the modal opens within 3s; the submit checks again anyway.
            refusal, _, _ = await asyncio.wait_for(self._appeal_check(guild, interaction.user), APPEAL_CHECK_SECONDS)
        except TimeoutError:
            log.warning("Appeal check was slow; opening the form anyway", extra={"event": "mod_appeal_check_slow"})
            refusal = None
        if refusal:
            await _reply(interaction, self._say(interaction, refusal, guild=guild.name))
            return

        async def submit(modal_interaction: discord.Interaction, text: str) -> None:
            await self._submit_appeal(modal_interaction, guild, text)

        await interaction.response.send_modal(
            TextModal(
                title=self._say(interaction, "appeal_title", guild=guild.name),
                custom_id=f"{APPEAL}-form:{guild.id}",
                label=self._say(interaction, "appeal_label"),
                min_length=20,
                max_length=1000,
                on_submit=submit,
            )
        )

    async def _submit_appeal(self, interaction: discord.Interaction, guild: discord.Guild, text: str) -> None:
        await interaction.response.defer(ephemeral=True, invisible=False)
        user = interaction.user
        refusal, ban, cases = await self._appeal_check(guild, user)  # again: two modals could be open
        if refusal:
            await interaction.followup.send(self._say(interaction, refusal, guild=guild.name), ephemeral=True)
            return
        try:
            case_id = await mod_cases.record_case(
                guild_id=guild.id, user_id=user.id, moderator_id=None, action="appeal", reason=text, source="appeal"
            )
        except Exception:
            log.exception("Couldn't record an appeal", extra={"event": "mod_appeal_record_failed", "user_id": user.id})
            await interaction.followup.send(self._say(interaction, "appeal_save_failed"), ephemeral=True)
            return
        channel = await mod_actions.staff_channel(self.bot, self.settings.moderation_appeals_channel_id)
        try:
            if channel is None:
                raise LookupError("no appeals or moderation log channel configured")
            await channel.send(
                view=_appeal_card(user, ban, text, cases, case_id),
                allowed_mentions=NO_MENTIONS,
            )
        except (discord.HTTPException, LookupError):
            # The case is saved, so staff still see it in the case list.
            log.exception("Couldn't post an appeal", extra={"event": "mod_appeal_post_failed", "user_id": user.id})
        await interaction.followup.send(self._say(interaction, "appeal_sent", guild=guild.name), ephemeral=True)

    async def _review(self, interaction: discord.Interaction, parts: list[str]) -> None:
        decision, user_id = parts[0], int(parts[1])
        if decision not in ("accept", "deny") or not await self._staff(interaction, "mod.ban"):
            return
        staff_message = interaction.message
        if decision == "accept":

            async def accept() -> None:
                await self._decide(interaction, user_id, staff_message, accepted=True, reason="")

            await while_busy(interaction, self._working, accept, failed=self._say(interaction, "appeal_failed"))
            return

        async def submit(modal_interaction: discord.Interaction, reason: str) -> None:
            message = modal_interaction.message or staff_message
            await self._decide(modal_interaction, user_id, message, accepted=False, reason=reason)

        await interaction.response.send_modal(
            TextModal(
                title=self._say(interaction, "deny_title"),
                custom_id=f"{APPEAL_REVIEW}-deny:{user_id}",
                label=self._say(interaction, "deny_label"),
                required=False,
                max_length=500,
                on_submit=submit,
            )
        )

    async def _decide(
        self,
        interaction: discord.Interaction,
        user_id: int,
        staff_message: discord.Message | None,
        *,
        accepted: bool,
        reason: str,
    ) -> None:
        await _start(interaction)
        guild = interaction.guild
        try:
            result = await mod_actions.perform(
                self.bot,
                guild,
                action="unban" if accepted else "note",
                target_id=user_id,
                moderator=interaction.user,
                reason="Appeal accepted" if accepted else f"Appeal denied: {reason or 'no reason given'}",
                source="appeal",
            )
        except mod_actions.ModActionError as error:
            await interaction.followup.send(error.text(reader_language(interaction, self.settings)), ephemeral=True)
            return await _put_back(interaction)
        if accepted:
            await self._dm(user_id, "appeal_accepted_dm", guild=guild.name)
        elif reason:
            await self._dm(user_id, "appeal_denied_dm", "appeal_denied_reason", guild=guild.name, reason=reason)
        else:
            await self._dm(user_id, "appeal_denied_dm", guild=guild.name)
        verdict = "Accepted" if accepted else "Denied"
        case = f" | case #{result.case_id}" if result.case_id else ""
        line = f"{verdict} by {interaction.user.mention}{case}" + (f": {reason}" if reason else "")
        await _mark_handled(staff_message, line)
        verdict_here = self._say(interaction, "accepted" if accepted else "denied")
        await interaction.followup.send(f"{verdict_here}{case}.", ephemeral=True)

    # --- member reports ---

    def _cooldown_left(self, user_id: int) -> int:
        last = self._last_report.get(user_id)
        return 0 if last is None else max(0, math.ceil(REPORT_COOLDOWN_SECONDS - (time.monotonic() - last)))

    @discord.message_command(name="Report message")
    async def report_message(self, ctx: discord.ApplicationContext, message: discord.Message):
        if ctx.guild_id != self.settings.panel_guild_id:
            return await ctx.respond(self._say(ctx, "report_main_only"), ephemeral=True)
        if message.author.bot:
            return await ctx.respond(self._say(ctx, "report_bot"), ephemeral=True)
        if message.author.id == ctx.author.id:
            return await ctx.respond(self._say(ctx, "report_own"), ephemeral=True)
        if wait := self._cooldown_left(ctx.author.id):
            return await ctx.respond(self._say(ctx, "report_wait", wait=wait), ephemeral=True)

        async def submit(modal_interaction: discord.Interaction, reason: str) -> None:
            await self._submit_report(modal_interaction, message, reason)

        await ctx.send_modal(
            TextModal(
                title=self._say(ctx, "report_title"),
                custom_id=f"modreport:{message.id}",
                label=self._say(ctx, "report_label"),
                required=False,
                max_length=500,
                on_submit=submit,
            )
        )

    async def _submit_report(self, interaction: discord.Interaction, message: discord.Message, reason: str) -> None:
        reporter = interaction.user
        if wait := self._cooldown_left(reporter.id):
            await _reply(interaction, self._say(interaction, "report_wait", wait=wait))
            return
        now = time.monotonic()
        self._last_report = {uid: at for uid, at in self._last_report.items() if now - at < REPORT_COOLDOWN_SECONDS}
        self._last_report[reporter.id] = now
        await interaction.response.defer(ephemeral=True, invisible=False)
        channel = await mod_actions.staff_channel(self.bot, self.settings.moderation_reports_channel_id)
        try:
            if channel is None:
                raise LookupError("no reports or moderation log channel configured")
            await channel.send(view=_report_card(reporter, message, reason), allowed_mentions=NO_MENTIONS)
        except (discord.HTTPException, LookupError):
            extra = {"event": "mod_report_post_failed", "message_id": message.id}
            log.exception("Couldn't post a message report", extra=extra)
            self._last_report.pop(reporter.id, None)
            await interaction.followup.send(self._say(interaction, "report_failed"), ephemeral=True)
            return
        try:
            await mod_cases.record_case(
                guild_id=interaction.guild.id,
                user_id=message.author.id,
                action="report",
                source="report",
                moderator_id=None,
                reason=f"Reported by <@{reporter.id}>: {reason or 'no reason given'}"[:500],
            )
        except Exception:
            log.exception("Couldn't record a report case", extra={"event": "mod_report_case_failed", "message_id": message.id})
        await interaction.followup.send(self._say(interaction, "report_sent"), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(ModInteractionsCog(bot))
