"""Staff Bulma's cards and forms (cogs/staff_assistant.py): consent card, onboarding modal, answer card, confirm cards
for proposed actions and their collapsed outcomes. Routed by custom id: staffai:setup, staffai:confirm:{id},
staffai:cancel:{id}, staffai_form:{new|edit}. Text follows the reader's language (en/es/pt)."""

from typing import Any
from zoneinfo import ZoneInfo

import discord

from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.ui.feedback_fix import _submitted
from bulmaai.ui.v2 import card, fit, trim
from bulmaai.utils.i18n import t, table

SETUP_ID = "staffai:setup"
CONFIRM, CANCEL = "confirm", "cancel"
FORM_PREFIX = "staffai_form:"
NEW, EDIT = "new", "edit"
FIELD_NICK, FIELD_LANG, FIELD_TZ, FIELD_TICKET, FIELD_CONSENT = "nickname", "language", "timezone", "ticket_info", "consent"
AUTO, YES, NO, AGREE = "auto", "yes", "no", "agree"
LANGUAGE_CHOICES = (("en", "English"), ("es", "Español"), ("pt", "Português"))
NICKNAME_MAX = 64
REPLY_LIMIT = 3900

TEXT = table({
    "en": {
        "consent": (
            "## 🧠 Staff Bulma\n"
            "Before I can help you, here is what I keep:\n"
            "- Every message you send in channels the Member role can see, from now on, plus your last ~100 there.\n"
            "- Your past moderation actions, read once to get to know you.\n\n"
            "I use this to know you better and answer in your style. It is kept until you run `/bulma forget`, "
            "`/bulma export` gives you a copy, and staff only ever see their own."
        ),
        "start": "Start setup",
        "modal_new": "Set up Bulma",
        "modal_edit": "Bulma settings",
        "nickname": "What should I call you?",
        "language": "Language",
        "auto": "Auto (from my language roles)",
        "timezone": "Timezone",
        "timezone_help": "A city like America/Sao_Paulo, or UTC / UTC-3.",
        "ticket_info": "Tickets",
        "ticket_yes": "Yes, mention who I am in tickets I help with",
        "ticket_no": "No",
        "consent_label": "Consent",
        "agree": "I agree",
        "bad_tz": "I don't know that timezone. Use a city like America/Sao_Paulo, or UTC / UTC-3.",
        "bad_offset": "Only whole-hour offsets work (UTC-3). For anything else, use a city name like Asia/Kolkata.",
        "bad_nick": "Pick a nickname.",
        "no_consent": "You need to agree to continue.",
        "done": "All set, {nickname}! Ask me anything with /bulma ask, in DMs, or by pinging me in staff channels.",
        "saved": "Saved, {nickname}.",
        "denied": "This is only for staff.",
        "setup_first": "Run /bulma ask first to set me up.",
        "forget_prompt": "Delete everything Bulma knows about you (messages, notes, reminders, MCP token)? This can't be undone.",
        "forget_yes": "Delete everything",
        "cancel": "Cancel",
        "cancelled": "Cancelled.",
        "forgot": "Done, I forgot everything. Run /bulma ask to set me up again.",
        "export": "Here is your data.",
        "connect_title": "## 🔌 Connect Claude to Bulma",
        "connect_body": (
            "Run this once in your terminal. The token is shown only now.\n```\n{command}\n```\n"
            "Endpoint: {url}\n-# Running `/bulma connect` again replaces the token; `/bulma forget` revokes it."
        ),
        "not_yours": "This isn't yours.",
        "expires": "Expires {when}",
        "confirm": "Confirm",
        "already_done": "Already done.",
        "already_cancelled": "Already cancelled.",
        "already_expired": "This one expired.",
        "already_failed": "That one already failed.",
        "no_reply": "(no answer)",
        "reminder": "⏰ {text}",
        "expired_card": "⌛ Expired\n-# {summary}",
    },
    "es": {
        "consent": (
            "## 🧠 Bulma para staff\n"
            "Antes de ayudarte, esto es lo que guardo:\n"
            "- Cada mensaje que envíes en canales que el rol Member puede ver, desde ahora, más tus últimos ~100 ahí.\n"
            "- Tus acciones de moderación pasadas, leídas una vez para conocerte.\n\n"
            "Lo uso para conocerte mejor y responder a tu estilo. Se conserva hasta que ejecutes `/bulma forget`, "
            "`/bulma export` te da una copia, y el staff solo ve lo suyo."
        ),
        "start": "Comenzar configuración",
        "modal_new": "Configura a Bulma",
        "modal_edit": "Ajustes de Bulma",
        "nickname": "¿Cómo te llamo?",
        "language": "Idioma",
        "auto": "Auto (según mis roles de idioma)",
        "timezone": "Zona horaria",
        "timezone_help": "Una ciudad como America/Sao_Paulo, o UTC / UTC-3.",
        "ticket_info": "Tickets",
        "ticket_yes": "Sí, menciona quién soy en los tickets que atiendo",
        "ticket_no": "No",
        "consent_label": "Consentimiento",
        "agree": "Estoy de acuerdo",
        "bad_tz": "No conozco esa zona horaria. Usa una ciudad como America/Sao_Paulo, o UTC / UTC-3.",
        "bad_offset": "Solo sirven desfases de horas enteras (UTC-3). Para otro, usa una ciudad como Asia/Kolkata.",
        "bad_nick": "Elige un apodo.",
        "no_consent": "Debes aceptar para continuar.",
        "done": "¡Listo, {nickname}! Pregúntame lo que quieras con /bulma ask, por DM o mencionándome en los canales del staff.",
        "saved": "Guardado, {nickname}.",
        "denied": "Esto es solo para el staff.",
        "setup_first": "Ejecuta /bulma ask primero para configurarme.",
        "forget_prompt": "¿Borrar todo lo que Bulma sabe de ti (mensajes, notas, recordatorios, token MCP)? No se puede deshacer.",
        "forget_yes": "Borrar todo",
        "cancel": "Cancelar",
        "cancelled": "Cancelado.",
        "forgot": "Listo, olvidé todo. Ejecuta /bulma ask para configurarme de nuevo.",
        "export": "Aquí están tus datos.",
        "connect_title": "## 🔌 Conecta Claude con Bulma",
        "connect_body": (
            "Ejecuta esto una vez en tu terminal. El token se muestra solo ahora.\n```\n{command}\n```\n"
            "Endpoint: {url}\n-# Si ejecutas `/bulma connect` otra vez, el token se reemplaza; `/bulma forget` lo revoca."
        ),
        "not_yours": "Esto no es tuyo.",
        "expires": "Expira {when}",
        "confirm": "Confirmar",
        "already_done": "Ya estaba hecho.",
        "already_cancelled": "Ya estaba cancelado.",
        "already_expired": "Esta ya expiró.",
        "already_failed": "Esta ya falló.",
        "no_reply": "(sin respuesta)",
        "reminder": "⏰ {text}",
        "expired_card": "⌛ Expiró\n-# {summary}",
    },
    "pt": {
        "consent": (
            "## 🧠 Bulma para staff\n"
            "Antes de te ajudar, isto é o que eu guardo:\n"
            "- Cada mensagem que você enviar em canais que o cargo Member pode ver, daqui em diante, mais as ~100 últimas lá.\n"
            "- Suas ações de moderação passadas, lidas uma vez para te conhecer.\n\n"
            "Uso isso para te conhecer melhor e responder no seu estilo. Fica guardado até você rodar `/bulma forget`, "
            "`/bulma export` te dá uma cópia, e a staff só vê o que é seu."
        ),
        "start": "Iniciar configuração",
        "modal_new": "Configurar a Bulma",
        "modal_edit": "Ajustes da Bulma",
        "nickname": "Como devo te chamar?",
        "language": "Idioma",
        "auto": "Auto (pelos meus cargos de idioma)",
        "timezone": "Fuso horário",
        "timezone_help": "Uma cidade como America/Sao_Paulo, ou UTC / UTC-3.",
        "ticket_info": "Tickets",
        "ticket_yes": "Sim, mencione quem eu sou nos tickets que atendo",
        "ticket_no": "Não",
        "consent_label": "Consentimento",
        "agree": "Eu concordo",
        "bad_tz": "Não conheço esse fuso. Use uma cidade como America/Sao_Paulo, ou UTC / UTC-3.",
        "bad_offset": "Só funcionam deslocamentos de horas inteiras (UTC-3). Para outro, use uma cidade como Asia/Kolkata.",
        "bad_nick": "Escolha um apelido.",
        "no_consent": "Você precisa concordar para continuar.",
        "done": "Tudo pronto, {nickname}! Pergunte o que quiser com /bulma ask, no DM ou me mencionando nos canais da staff.",
        "saved": "Salvo, {nickname}.",
        "denied": "Isso é só para a staff.",
        "setup_first": "Rode /bulma ask primeiro para me configurar.",
        "forget_prompt": "Apagar tudo o que a Bulma sabe sobre você (mensagens, notas, lembretes, token MCP)? Não dá para desfazer.",
        "forget_yes": "Apagar tudo",
        "cancel": "Cancelar",
        "cancelled": "Cancelado.",
        "forgot": "Pronto, esqueci tudo. Rode /bulma ask para me configurar de novo.",
        "export": "Aqui estão seus dados.",
        "connect_title": "## 🔌 Conecte o Claude à Bulma",
        "connect_body": (
            "Rode isto uma vez no seu terminal. O token aparece só agora.\n```\n{command}\n```\n"
            "Endpoint: {url}\n-# Rodar `/bulma connect` de novo substitui o token; `/bulma forget` o revoga."
        ),
        "not_yours": "Isso não é seu.",
        "expires": "Expira {when}",
        "confirm": "Confirmar",
        "already_done": "Já estava feito.",
        "already_cancelled": "Já estava cancelado.",
        "already_expired": "Este já expirou.",
        "already_failed": "Este já falhou.",
        "no_reply": "(sem resposta)",
        "reminder": "⏰ {text}",
        "expired_card": "⌛ Expirou\n-# {summary}",
    },
})

ACTION_COLOR = discord.Color.gold()
DONE_COLOR = discord.Color.green()
FAILED_COLOR = discord.Color.red()
GREY = discord.Color.dark_grey()


def confirm_id(action: str, action_id: int) -> str:
    return f"staffai:{action}:{action_id}"


def parse_custom_id(value: str) -> tuple[str, int] | None:
    """(confirm or cancel, action id) for a confirm card's button."""
    parts = value.split(":")
    if len(parts) == 3 and parts[0] == "staffai" and parts[1] in (CONFIRM, CANCEL) and parts[2].isdigit():
        return parts[1], int(parts[2])
    return None


def form_id(mode: str) -> str:
    return f"{FORM_PREFIX}{mode}"


def parse_form_id(value: str) -> str | None:
    mode = value.removeprefix(FORM_PREFIX) if value.startswith(FORM_PREFIX) else ""
    return mode if mode in (NEW, EDIT) else None


def parse_timezone(raw: str) -> tuple[str | None, str | None]:
    """(IANA name, None) or (None, error key). 'UTC-3' becomes 'Etc/GMT+3' (POSIX sign); 'UTC' and 'UTC+0' are UTC."""
    value = (raw or "").strip() or "UTC"
    upper = value.upper().replace(" ", "")
    if upper in ("UTC", "GMT", "Z"):
        return "UTC", None
    if upper.startswith(("UTC", "GMT")) and len(upper) > 3 and upper[3] in "+-":
        sign, number = upper[3], upper[4:]
        if not number or not all(ch.isdigit() or ch == ":" for ch in number):
            return None, "bad_tz"
        hours, _, minutes = number.partition(":")
        if not hours.isdigit() or (minutes and not minutes.isdigit()):
            return None, "bad_tz"
        if int(minutes or 0) != 0:
            return None, "bad_offset"
        offset = int(hours)
        if offset == 0:
            return "UTC", None
        name = f"Etc/GMT{'+' if sign == '-' else '-'}{offset}"
        try:
            ZoneInfo(name)
        except Exception:
            return None, "bad_tz"
        return name, None
    if "/" not in value and value.upper() != "UTC":
        return None, "bad_tz"
    try:
        ZoneInfo(value)
    except Exception:
        return None, "bad_tz"
    return value, None


def consent_card(language: str) -> discord.ui.DesignerView:
    button = discord.ui.Button(label=t(TEXT, "start", language), style=discord.ButtonStyle.primary, custom_id=SETUP_ID)
    return card(t(TEXT, "consent", language), buttons=[button], color=ACTION_COLOR)


def setup_modal(mode: str, language: str, *, nickname: str = "", stored_language: str | None = None,
                timezone: str = "UTC", ticket_info: bool = False) -> discord.ui.DesignerModal:
    nick_input = discord.ui.InputText(
        style=discord.InputTextStyle.short, custom_id=FIELD_NICK, max_length=NICKNAME_MAX, value=nickname[:NICKNAME_MAX] or None,
        required=True,
    )
    lang_select = discord.ui.Select(
        custom_id=FIELD_LANG,
        options=[discord.SelectOption(label=t(TEXT, "auto", language), value=AUTO, default=stored_language is None)]
        + [discord.SelectOption(label=name, value=code, default=stored_language == code) for code, name in LANGUAGE_CHOICES],
        required=True,
    )
    tz_input = discord.ui.InputText(
        style=discord.InputTextStyle.short, custom_id=FIELD_TZ, max_length=64, value=timezone or "UTC", required=True
    )
    ticket_select = discord.ui.Select(
        custom_id=FIELD_TICKET,
        options=[
            discord.SelectOption(label=t(TEXT, "ticket_yes", language), value=YES, default=ticket_info),
            discord.SelectOption(label=t(TEXT, "ticket_no", language), value=NO, default=not ticket_info),
        ],
        required=True,
    )
    items = [
        discord.ui.Label(t(TEXT, "nickname", language), nick_input),
        discord.ui.Label(t(TEXT, "language", language), lang_select),
        discord.ui.Label(t(TEXT, "timezone", language), tz_input, description=t(TEXT, "timezone_help", language)),
        discord.ui.Label(t(TEXT, "ticket_info", language), ticket_select),
    ]
    if mode == NEW:
        consent_select = discord.ui.Select(
            custom_id=FIELD_CONSENT,
            options=[discord.SelectOption(label=t(TEXT, "agree", language), value=AGREE)],
            min_values=1,
            max_values=1,
            required=True,
        )
        items.append(discord.ui.Label(t(TEXT, "consent_label", language), consent_select))
    return discord.ui.DesignerModal(
        *items, title=trim(t(TEXT, f"modal_{mode}", language), 45), custom_id=form_id(mode), store=False
    )


def read_form(components: Any) -> dict[str, Any]:
    """The onboarding form's raw values: nickname, language ('auto' or a code), timezone, ticket_info (bool),
    consent (bool)."""
    values = _submitted(components)

    def picked(name: str) -> str:
        value = values.get(name)
        return str(value[0]) if isinstance(value, list) and value else ""

    return {
        "nickname": str(values.get(FIELD_NICK) or "").strip(),
        "language": picked(FIELD_LANG) or AUTO,
        "timezone": str(values.get(FIELD_TZ) or "").strip(),
        "ticket_info": picked(FIELD_TICKET) == YES,
        "consent": picked(FIELD_CONSENT) == AGREE,
    }


def reply_view(reply: Any) -> discord.ui.DesignerView:
    language = getattr(reply, "language", "en")
    body = defuse_mentions(reply.text or "") or t(TEXT, "no_reply", language)
    return card(*fit([body], REPLY_LIMIT))


def confirm_view(row: dict, language: str) -> discord.ui.DesignerView:
    expires = row.get("expires_at")
    parts = [defuse_mentions(trim(row["summary"], 1500))]
    if expires is not None:
        parts.append(f"-# {t(TEXT, 'expires', language, when=f'<t:{int(expires.timestamp())}:R>')}")
    buttons = [
        discord.ui.Button(
            label=t(TEXT, "confirm", language), style=discord.ButtonStyle.danger, custom_id=confirm_id(CONFIRM, row["id"])
        ),
        discord.ui.Button(
            label=t(TEXT, "cancel", language), style=discord.ButtonStyle.secondary, custom_id=confirm_id(CANCEL, row["id"])
        ),
    ]
    return card(*parts, buttons=buttons, color=ACTION_COLOR)


def done_card(summary: str, result: str | None) -> discord.ui.DesignerView:
    lines = [f"✅ {defuse_mentions(trim(summary, 1500))}"]
    if result:
        lines.append(f"-# {defuse_mentions(trim(result, 1500))}")
    return card("\n".join(lines), color=DONE_COLOR)


def failed_card(summary: str, error: str | None) -> discord.ui.DesignerView:
    lines = [f"❌ {defuse_mentions(trim(summary, 1500))}"]
    if error:
        lines.append(f"-# {defuse_mentions(trim(error, 1500))}")
    return card("\n".join(lines), color=FAILED_COLOR)


def cancelled_card(summary: str, language: str) -> discord.ui.DesignerView:
    return card(f"🚫 {defuse_mentions(trim(summary, 1500))}\n-# {t(TEXT, 'cancelled', language)}", color=GREY)


def expired_card(summary: str, language: str) -> discord.ui.DesignerView:
    return card(t(TEXT, "expired_card", language, summary=defuse_mentions(trim(summary, 1500))), color=GREY)


def outcome_card(row: dict, language: str) -> discord.ui.DesignerView:
    """The collapsed card for an action's current outcome."""
    outcome = row.get("outcome")
    if outcome == "done":
        return done_card(row["summary"], row.get("result"))
    if outcome == "failed":
        return failed_card(row["summary"], row.get("result"))
    if outcome == "cancelled":
        return cancelled_card(row["summary"], language)
    return expired_card(row["summary"], language)


def outcome_text(row: dict | None, language: str) -> str:
    key = {"done": "already_done", "cancelled": "already_cancelled", "failed": "already_failed"}.get(
        (row or {}).get("outcome"), "already_expired"
    )
    return t(TEXT, key, language)


class ForgetView(discord.ui.DesignerView):
    """Private "delete everything?" prompt; on_confirm(interaction) does the deleting and answers."""

    def __init__(self, language: str, on_confirm):
        confirm = discord.ui.Button(label=t(TEXT, "forget_yes", language), style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label=t(TEXT, "cancel", language), style=discord.ButtonStyle.secondary)
        confirm.callback, cancel.callback = self._confirm, self._cancel
        super().__init__(
            discord.ui.Container(
                discord.ui.TextDisplay(t(TEXT, "forget_prompt", language)),
                discord.ui.ActionRow(confirm, cancel),
                color=ACTION_COLOR,
            ),
            timeout=120,
            disable_on_timeout=True,
        )
        self._on_confirm, self._language = on_confirm, language

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self._on_confirm(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=card(t(TEXT, "cancelled", self._language), color=GREY))
