"""Ticket panel, intake modals and the in-ticket Components V2 cards (cogs/tickets.py does the work).

The public panel is English with a short Spanish/Portuguese line. Each member's form and ephemeral replies are in their
language (one language role, or the one they pick on form_language_card), which becomes the ticket's support language.
Member-facing messages inside a ticket and its DMs are in that language only (TEXT, CATEGORY_TEXT: one table entry per
language). Ephemeral replies to staff are in the reader's own language (utils/i18n.user_language); public staff
buttons and notices in the ticket are English. Buttons are stateless: TicketsCog.on_interaction routes them by
custom id and looks the ticket up by channel, so they survive bot restarts.
"""

from dataclasses import dataclass
from datetime import datetime

import discord

from bulmaai.ui.v2 import already_working, busy, card, facts, trim
from bulmaai.utils import i18n

PANEL_SELECT_ID = "ticket_panel_select"
CLOSE_BUTTON_ID = "ticket_btn_close"
TRANSCRIPT_BUTTON_ID = "ticket_btn_transcript"
REOPEN_BUTTON_ID = "ticket_btn_reopen"
DELETE_BUTTON_ID = "ticket_btn_delete"
AI_BUTTON_ID = "ticket_btn_ai"
SOLVED_BUTTON_ID = "ticket_btn_solved"
# Staff copilot (ui/staff_copilot): the ticket so far, and a draft of the next reply to the owner; both ephemeral.
SUMMARY_BUTTON_ID = "ticket_btn_summary"
DRAFT_BUTTON_ID = "ticket_btn_draft"
# + ticket id; the Re-open button older "solved" DMs still carry. It now only answers that staff re-open tickets.
SOLVED_REOPEN_PREFIX = "ticket_solved_reopen:"
ACTIONS_ID = 800  # the button row on the welcome / closed cards
LANG_SELECT_ID = "ticket_lang_select"
# + language code; the language picker posted in a new ticket whose owner has several language roles.
LANG_BUTTON_PREFIX = "ticket_lang:"
LANG_ROW_ID = 801  # the support-language select on the welcome card
# + "{category key}:{language}"; the language buttons the panel answers with when a member has no or several
# language roles. The click opens that category's form in that language.
FORM_BUTTON_PREFIX = "ticket_form:"
COG_NAME = "TicketsCog"

LANGUAGES = {"en": ("🇬🇧", "English"), "es": ("🇪🇸", "Español"), "pt": ("🇧🇷", "Português")}

# Member-facing ticket messages, each in the ticket's support language.
TEXT: dict[str, dict[str, str]] = i18n.table({
    "en": {
        "greeting": "Hey {mention}! Staff will be with you soon. Add screenshots or extra details while you wait.",
        "no_pings": "⚠️ **Don't ping staff, it gets you a warn.**",
        "open_ticket": "Open ticket",
        "view_transcript": "View transcript",
        "created": "Your ticket has been created. Staff will answer you there, no need to ping them.",
        "transcript": "Here is the transcript of your ticket.",
        "expires": "It expires {when}.",
        "kept": "It is kept permanently.",
        "solved": "Ticket #{number} was marked as solved and closed.",
        "solved_until": "Your answer is in the transcript; it's available until {until}.",
        "solved_kept": "Your answer is in the transcript; it's kept permanently.",
        "solved_deletes": "The ticket will be deleted {deadline}.",
        "reopen_staff": "Only staff can re-open a ticket. If you still need help, please open a new ticket.",
        "deleting": "This ticket will be deleted {when}.",
        "reminder_title": "your ticket is waiting for your reply",
        "reminder": "Your ticket got an answer and is waiting for yours. Reply there so we can keep helping you.",
        "pick_language": "Which language do you want support in?",
        "language_set": "Support language: {name}.",
        "language_changed": "Support language changed to {name} by {mention}.",
        "creating": "🎫 Creating your ticket…",
        "failed": "❌ Something went wrong creating your ticket. Please try again or contact staff.",
        "still_creating": "Your ticket is still being created. Please wait a moment.",
        "already_open": "You already have an open ticket: <#{channel_id}>. Please continue there.",
        "ready": "✅ Your ticket is ready: {channel}",
        "no_permission": "You can't do that here.",
        "close": "Close",
        "cancel": "Cancel",
        "cancelled": "Cancelled.",
        "close_title": "Close this ticket?",
        "close_body": "Once closed, only staff can see this channel. A transcript of the conversation is shown here and sent by DM.",
        "reopened": "Ticket re-opened",
        "reopened_by": "Re-opened by {mention}.",
        "transcript_of": "Transcript of ticket #{number}",
        "staff_only": "Only staff can do this.",
        "mod_only": "Only moderators and admins can do this.",
        "not_ticket": "This isn't an open ticket channel.",
        "already_closed": "This ticket is already closed.",
        "not_closed": "This ticket isn't closed.",
        "owner_left": "The ticket owner left the server, so it can't be re-opened.",
        "delete_failed": "❌ I couldn't delete this channel. Check my Manage Channels permission.",
        "transcript_failed": "❌ I couldn't generate the transcript.",
        "protected_target": "You can't remove the ticket owner or staff.",
        "ai_paused": "AI is paused for today.",
        "copilot_failed": "I couldn't do that right now. Please try again.",
        "post_failed": "I couldn't post that reply. Please try again.",
        "assign_bot": "You can't assign a ticket to a bot.",
        "assigned": "📌 {mention}, this ticket was assigned to you by {by}. Please take care of it.",
        "assigned_dm": "{by} assigned this ticket to you. Please take care of it.",
    },
    "es": {
        "greeting": "¡Hola {mention}! El staff te atenderá pronto. Puedes añadir capturas o más detalles mientras esperas.",
        "no_pings": "⚠️ **No menciones al staff, te dará una advertencia.**",
        "open_ticket": "Abrir ticket",
        "view_transcript": "Ver transcripción",
        "created": "Tu ticket ha sido creado. El staff te responderá allí, no hace falta mencionarlos.",
        "transcript": "Aquí tienes la transcripción de tu ticket.",
        "expires": "Expira {when}.",
        "kept": "Se guarda para siempre.",
        "solved": "El ticket #{number} se marcó como resuelto y se cerró.",
        "solved_until": "Tu respuesta está en la transcripción; disponible hasta {until}.",
        "solved_kept": "Tu respuesta está en la transcripción; se guarda para siempre.",
        "solved_deletes": "El ticket se eliminará {deadline}.",
        "reopen_staff": "Solo el staff puede reabrir un ticket. Si aún necesitas ayuda, abre un ticket nuevo.",
        "deleting": "Este ticket se eliminará {when}.",
        "reminder_title": "tu ticket espera tu respuesta",
        "reminder": "Tu ticket recibió una respuesta y espera la tuya. Responde allí para que podamos seguir ayudándote.",
        "pick_language": "¿En qué idioma quieres recibir soporte?",
        "language_set": "Idioma de soporte: {name}.",
        "language_changed": "Idioma de soporte cambiado a {name} por {mention}.",
        "creating": "🎫 Creando tu ticket…",
        "failed": "❌ Algo salió mal al crear tu ticket. Inténtalo de nuevo o contacta al staff.",
        "still_creating": "Tu ticket todavía se está creando. Espera un momento.",
        "already_open": "Ya tienes un ticket abierto: <#{channel_id}>. Continúa allí.",
        "ready": "✅ Tu ticket está listo: {channel}",
        "no_permission": "No puedes hacer eso aquí.",
        "close": "Cerrar",
        "cancel": "Cancelar",
        "cancelled": "Cancelado.",
        "close_title": "¿Cerrar este ticket?",
        "close_body": "Después de cerrarlo, solo el staff podrá ver este canal. La transcripción de la conversación aparece aquí y te llega por MD.",
        "reopened": "Ticket reabierto",
        "reopened_by": "Reabierto por {mention}.",
        "transcript_of": "Transcripción del ticket #{number}",
        "staff_only": "Solo el staff puede hacer esto.",
        "mod_only": "Solo los moderadores y admins pueden hacer esto.",
        "not_ticket": "Este no es un canal de ticket abierto.",
        "already_closed": "Este ticket ya está cerrado.",
        "not_closed": "Este ticket no está cerrado.",
        "owner_left": "El dueño del ticket salió del servidor, así que no se puede reabrir.",
        "delete_failed": "❌ No pude eliminar este canal. Revisa mi permiso de Gestionar canales.",
        "transcript_failed": "❌ No pude generar la transcripción.",
        "protected_target": "No puedes quitar al dueño del ticket ni al staff.",
        "ai_paused": "La IA está en pausa por hoy.",
        "copilot_failed": "No pude hacer eso ahora. Inténtalo de nuevo.",
        "post_failed": "No pude publicar esa respuesta. Inténtalo de nuevo.",
        "assign_bot": "No puedes asignar un ticket a un bot.",
        "assigned": "📌 {mention}, {by} te asignó este ticket. Por favor, encárgate de él.",
        "assigned_dm": "{by} te asignó este ticket. Por favor, encárgate de él.",
    },
    "pt": {
        "greeting": "Olá {mention}! A staff vai te atender em breve. Adicione capturas ou mais detalhes enquanto espera.",
        "no_pings": "⚠️ **Não marque a staff, isso te dá uma advertência.**",
        "open_ticket": "Abrir ticket",
        "view_transcript": "Ver transcrição",
        "created": "Seu ticket foi criado. A staff vai te responder lá, não precisa marcá-los.",
        "transcript": "Aqui está a transcrição do seu ticket.",
        "expires": "Ela expira {when}.",
        "kept": "Ela é guardada para sempre.",
        "solved": "O ticket #{number} foi marcado como resolvido e fechado.",
        "solved_until": "Sua resposta está na transcrição; disponível até {until}.",
        "solved_kept": "Sua resposta está na transcrição; ela é guardada para sempre.",
        "solved_deletes": "O ticket será excluído {deadline}.",
        "reopen_staff": "Só a staff pode reabrir um ticket. Se ainda precisar de ajuda, abra um novo ticket.",
        "deleting": "Este ticket será excluído {when}.",
        "reminder_title": "seu ticket está esperando sua resposta",
        "reminder": "Seu ticket recebeu uma resposta e está esperando a sua. Responda lá para continuarmos te ajudando.",
        "pick_language": "Em qual idioma você quer receber suporte?",
        "language_set": "Idioma do suporte: {name}.",
        "language_changed": "Idioma do suporte alterado para {name} por {mention}.",
        "creating": "🎫 Criando seu ticket…",
        "failed": "❌ Algo deu errado ao criar seu ticket. Tente novamente ou fale com a staff.",
        "still_creating": "Seu ticket ainda está sendo criado. Aguarde um momento.",
        "already_open": "Você já tem um ticket aberto: <#{channel_id}>. Continue nele.",
        "ready": "✅ Seu ticket está pronto: {channel}",
        "no_permission": "Você não pode fazer isso aqui.",
        "close": "Fechar",
        "cancel": "Cancelar",
        "cancelled": "Cancelado.",
        "close_title": "Fechar este ticket?",
        "close_body": "Depois de fechado, só a staff pode ver este canal. A transcrição da conversa aparece aqui e é enviada por DM.",
        "reopened": "Ticket reaberto",
        "reopened_by": "Reaberto por {mention}.",
        "transcript_of": "Transcrição do ticket #{number}",
        "staff_only": "Só a staff pode fazer isso.",
        "mod_only": "Só moderadores e admins podem fazer isso.",
        "not_ticket": "Este não é um canal de ticket aberto.",
        "already_closed": "Este ticket já está fechado.",
        "not_closed": "Este ticket não está fechado.",
        "owner_left": "O dono do ticket saiu do servidor, então ele não pode ser reaberto.",
        "delete_failed": "❌ Não consegui excluir este canal. Verifique minha permissão de Gerenciar canais.",
        "transcript_failed": "❌ Não consegui gerar a transcrição.",
        "protected_target": "Você não pode remover o dono do ticket nem a staff.",
        "ai_paused": "A IA está pausada por hoje.",
        "copilot_failed": "Não consegui fazer isso agora. Tente novamente.",
        "post_failed": "Não consegui publicar essa resposta. Tente novamente.",
        "assign_bot": "Você não pode atribuir um ticket a um bot.",
        "assigned": "📌 {mention}, {by} atribuiu este ticket a você. Por favor, cuide dele.",
        "assigned_dm": "{by} atribuiu este ticket a você. Por favor, cuide dele.",
    },
})


def t(language: str, key: str, **values) -> str:
    """TEXT in language (English for an unknown one)."""
    return i18n.t(TEXT, key, language, **values)

BLURPLE = discord.Color.from_rgb(88, 101, 242)
CLOSED_RED = discord.Color.from_rgb(237, 66, 69)
REOPENED_GREEN = discord.Color.from_rgb(87, 242, 135)
REQUEST_GREY = discord.Color.from_rgb(79, 84, 92)


@dataclass(frozen=True, slots=True)
class FormField:
    """A form input's shape; its label and placeholder per language are in CATEGORY_TEXT."""

    paragraph: bool = False
    required: bool = True
    max_length: int = 100


@dataclass(frozen=True, slots=True)
class CategoryText:
    """A category's member-facing words in one language. fields: (label, placeholder) per FormField, same order.
    Discord limits: label/description 100, title and field labels 45, placeholders 100 characters."""

    label: str
    description: str
    title: str
    fields: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TicketCategory:
    key: str
    emoji: str
    fallback_slug: str
    ai_reply: bool
    fields: tuple[FormField, ...]
    # Moderators and up only: Helpers don't see the channel, and an escalation pings moderators instead of helpers.
    mod_only: bool = False

    def text(self, language: str) -> CategoryText:
        """The category's words in language (English for an unknown one)."""
        return CATEGORY_TEXT.get(language, CATEGORY_TEXT["en"])[self.key]

    @property
    def name(self) -> str:
        """English name, for everything inside a ticket."""
        return self.text("en").label

    @property
    def description(self) -> str:
        """English description (the panel and the AI's open_ticket tool)."""
        return self.text("en").description


# The panel lists them in this order; Other stays last.
CATEGORIES: dict[str, TicketCategory] = {
    category.key: category
    for category in (
        TicketCategory(
            key="bug",
            emoji="⚙️",
            fallback_slug="bug",
            ai_reply=True,
            fields=(
                FormField(),
                FormField(paragraph=True, max_length=1000),
                FormField(paragraph=True, required=False, max_length=800),
                FormField(),
                FormField(required=False, max_length=200),
            ),
        ),
        TicketCategory(
            key="patreon",
            emoji="💎",
            fallback_slug="patreon",
            ai_reply=True,
            fields=(FormField(), FormField(), FormField(paragraph=True, max_length=1000)),
        ),
        TicketCategory(
            key="install",
            emoji="📦",
            fallback_slug="install",
            ai_reply=True,
            fields=(FormField(), FormField(), FormField(paragraph=True, max_length=1000)),
        ),
        TicketCategory(
            key="server",
            emoji="🖥️",
            fallback_slug="server",
            ai_reply=True,
            fields=(FormField(), FormField(), FormField(paragraph=True, max_length=1000)),
        ),
        TicketCategory(
            key="report",
            emoji="🚨",
            fallback_slug="report",
            ai_reply=False,
            mod_only=True,
            fields=(
                FormField(),
                FormField(paragraph=True, max_length=1000),
                FormField(paragraph=True, required=False, max_length=500),
            ),
        ),
        TicketCategory(
            key="appeal",
            emoji="⚖️",
            fallback_slug="appeal",
            ai_reply=False,
            mod_only=True,
            fields=(FormField(max_length=200), FormField(paragraph=True, max_length=1000)),
        ),
        TicketCategory(
            key="partner",
            emoji="🤝",
            fallback_slug="partner",
            ai_reply=False,
            fields=(FormField(max_length=200), FormField(), FormField(paragraph=True, max_length=1000)),
        ),
        TicketCategory(
            key="contribute",
            emoji="🙌",
            fallback_slug="contribute",
            ai_reply=False,
            fields=(
                FormField(),
                FormField(paragraph=True, max_length=800),
                FormField(required=False, max_length=200),
            ),
        ),
        TicketCategory(
            key="other",
            emoji="💠",
            fallback_slug="help",
            ai_reply=True,
            fields=(FormField(), FormField(paragraph=True, max_length=1000)),
        ),
    )
}

# Every category's words, per language. A new language is one more entry here and in TEXT and LANGUAGES.
CATEGORY_TEXT: dict[str, dict[str, CategoryText]] = i18n.table({
    "en": {
        "bug": CategoryText(
            "Game-Breaking Bug",
            "Crashes, corrupted worlds",
            "Bug Report",
            (
                ("Summary", "Crash when..."),
                ("What happened?", "Describe it"),
                ("Steps to reproduce", "1. ...\n2. ..."),
                ("Mod & Minecraft version", "DMZ 2.2 / MC 1.20.1"),
                ("Other mods", "Optional"),
            ),
        ),
        "patreon": CategoryText(
            "Patreon & Beta Access",
            "Whitelist not working, account linking, dev jar access",
            "Patreon & Beta Access",
            (
                ("Patreon email or name", "The email or name on your Patreon account"),
                ("Minecraft username", "Your in-game name"),
                ("What's wrong?", "Describe the problem"),
            ),
        ),
        "install": CategoryText(
            "Installation & Setup",
            "Won't launch (no crash), which version, modpack help",
            "Installation & Setup",
            (
                ("Launcher", "CurseForge, Modrinth, Prism, vanilla..."),
                ("Mod & Forge version", "DMZ 2.2 / Forge 47.3.0"),
                ("What did you try?", "What happens, and what you tried so far"),
            ),
        ),
        "server": CategoryText(
            "Server & Multiplayer",
            "For server owners: configs, permissions, commands, sync",
            "Server & Multiplayer",
            (
                ("Server type", "Host (which one), self-hosted or LAN"),
                ("Versions", "DMZ 2.2 / Forge 47.3.0 / MC 1.20.1"),
                ("The issue", "What's going wrong?"),
            ),
        ),
        "report": CategoryText(
            "Report a Member",
            "Scams, harassment, DM ads",
            "Report a Member",
            (
                ("Who? (username or ID)", "Their username or user ID"),
                ("What happened?", "Tell us what they did"),
                ("Proof (links)", "Message or screenshot links (you can also upload them in the ticket)"),
            ),
        ),
        "appeal": CategoryText(
            "Appeal a Punishment",
            "Warns and timeouts. Bans go through the ban appeal website",
            "Appeal a Punishment",
            (
                ("Which punishment?", "Warn or timeout, when, and the reason given"),
                ("Why should it be lifted?", "Explain"),
            ),
        ),
        "partner": CategoryText(
            "Partnership & Creators",
            "Partnerships, YouTubers and streamers",
            "Partnership & Creators",
            (
                ("Channel or server link", "https://..."),
                ("Audience size", "Subscribers, followers or members"),
                ("Proposal", "What do you have in mind?"),
            ),
        ),
        "contribute": CategoryText(
            "Contributing to the Mod",
            "Code, art, translations",
            "Contribute",
            (
                ("How to help?", "Code, art, translation..."),
                ("Experience", "Tell us about yourself"),
                ("Links (GitHub, portfolio)", "Optional"),
            ),
        ),
        "other": CategoryText(
            "Other",
            "Anything else",
            "Other",
            (("Subject", "What is it about?"), ("Details", "Explain")),
        ),
    },
    "es": {
        "bug": CategoryText(
            "Bug que rompe el juego",
            "Cierres, mundos dañados",
            "Reporte de bug",
            (
                ("Resumen", "Se cierra al..."),
                ("¿Qué pasó?", "Descríbelo"),
                ("Pasos para reproducirlo", "1. ...\n2. ..."),
                ("Versión del mod y de Minecraft", "DMZ 2.2 / MC 1.20.1"),
                ("Otros mods", "Opcional"),
            ),
        ),
        "patreon": CategoryText(
            "Patreon y acceso beta",
            "La whitelist no funciona, vinculación de cuenta, acceso a dev jars",
            "Patreon y acceso beta",
            (
                ("Correo o nombre de Patreon", "El correo o nombre de tu cuenta de Patreon"),
                ("Usuario de Minecraft", "Tu nombre en el juego"),
                ("¿Qué pasa?", "Describe el problema"),
            ),
        ),
        "install": CategoryText(
            "Instalación y configuración",
            "No abre (sin crash), qué versión usar, ayuda con modpacks",
            "Instalación y configuración",
            (
                ("Launcher", "CurseForge, Modrinth, Prism, vanilla..."),
                ("Versión del mod y de Forge", "DMZ 2.2 / Forge 47.3.0"),
                ("¿Qué intentaste?", "Qué pasa y qué intentaste hasta ahora"),
            ),
        ),
        "server": CategoryText(
            "Servidor y multijugador",
            "Para dueños de servidores: configs, permisos, comandos, sincronización",
            "Servidor y multijugador",
            (
                ("Tipo de servidor", "Host (cuál), propio o LAN"),
                ("Versiones", "DMZ 2.2 / Forge 47.3.0 / MC 1.20.1"),
                ("El problema", "¿Qué está fallando?"),
            ),
        ),
        "report": CategoryText(
            "Reportar a un miembro",
            "Estafas, acoso, publicidad por MD",
            "Reportar a un miembro",
            (
                ("¿Quién? (usuario o ID)", "Su nombre de usuario o ID"),
                ("¿Qué pasó?", "Cuéntanos qué hizo"),
                ("Pruebas (enlaces)", "Enlaces a mensajes o capturas (también puedes subirlas en el ticket)"),
            ),
        ),
        "appeal": CategoryText(
            "Apelar una sanción",
            "Advertencias y timeouts. Los baneos van por la web de apelaciones",
            "Apelar una sanción",
            (
                ("¿Qué sanción?", "Advertencia o timeout, cuándo y el motivo que te dieron"),
                ("¿Por qué debería quitarse?", "Explícalo"),
            ),
        ),
        "partner": CategoryText(
            "Alianzas y creadores",
            "Alianzas, YouTubers y streamers",
            "Alianzas y creadores",
            (
                ("Enlace del canal o servidor", "https://..."),
                ("Tamaño de la audiencia", "Suscriptores, seguidores o miembros"),
                ("Propuesta", "¿Qué tienes en mente?"),
            ),
        ),
        "contribute": CategoryText(
            "Contribuir al mod",
            "Código, arte, traducciones",
            "Contribuir",
            (
                ("¿Cómo quieres ayudar?", "Código, arte, traducción..."),
                ("Experiencia", "Cuéntanos de ti"),
                ("Enlaces (GitHub, portafolio)", "Opcional"),
            ),
        ),
        "other": CategoryText(
            "Otro",
            "Cualquier otra cosa",
            "Otro",
            (("Asunto", "¿De qué se trata?"), ("Detalles", "Explícalo")),
        ),
    },
    "pt": {
        "bug": CategoryText(
            "Bug que quebra o jogo",
            "Crashes, mundos corrompidos",
            "Relatório de bug",
            (
                ("Resumo", "Fecha ao..."),
                ("O que aconteceu?", "Descreva"),
                ("Passos para reproduzir", "1. ...\n2. ..."),
                ("Versão do mod e do Minecraft", "DMZ 2.2 / MC 1.20.1"),
                ("Outros mods", "Opcional"),
            ),
        ),
        "patreon": CategoryText(
            "Patreon e acesso beta",
            "Whitelist não funciona, vinculação de conta, acesso a dev jars",
            "Patreon e acesso beta",
            (
                ("E-mail ou nome do Patreon", "O e-mail ou nome da sua conta do Patreon"),
                ("Usuário do Minecraft", "Seu nome no jogo"),
                ("Qual é o problema?", "Descreva o problema"),
            ),
        ),
        "install": CategoryText(
            "Instalação e configuração",
            "Não abre (sem crash), qual versão usar, ajuda com modpacks",
            "Instalação e configuração",
            (
                ("Launcher", "CurseForge, Modrinth, Prism, vanilla..."),
                ("Versão do mod e do Forge", "DMZ 2.2 / Forge 47.3.0"),
                ("O que você tentou?", "O que acontece e o que você já tentou"),
            ),
        ),
        "server": CategoryText(
            "Servidor e multiplayer",
            "Para donos de servidor: configs, permissões, comandos, sincronização",
            "Servidor e multiplayer",
            (
                ("Tipo de servidor", "Host (qual), próprio ou LAN"),
                ("Versões", "DMZ 2.2 / Forge 47.3.0 / MC 1.20.1"),
                ("O problema", "O que está dando errado?"),
            ),
        ),
        "report": CategoryText(
            "Denunciar um membro",
            "Golpes, assédio, divulgação por DM",
            "Denunciar um membro",
            (
                ("Quem? (usuário ou ID)", "O nome de usuário ou ID da pessoa"),
                ("O que aconteceu?", "Conte o que a pessoa fez"),
                ("Provas (links)", "Links de mensagens ou prints (você também pode enviá-los no ticket)"),
            ),
        ),
        "appeal": CategoryText(
            "Recorrer de uma punição",
            "Advertências e timeouts. Banimentos vão pelo site de recurso",
            "Recorrer de uma punição",
            (
                ("Qual punição?", "Advertência ou timeout, quando e o motivo informado"),
                ("Por que deveria ser retirada?", "Explique"),
            ),
        ),
        "partner": CategoryText(
            "Parcerias e criadores",
            "Parcerias, YouTubers e streamers",
            "Parcerias e criadores",
            (
                ("Link do canal ou servidor", "https://..."),
                ("Tamanho do público", "Inscritos, seguidores ou membros"),
                ("Proposta", "O que você tem em mente?"),
            ),
        ),
        "contribute": CategoryText(
            "Contribuir com o mod",
            "Código, arte, traduções",
            "Contribuir",
            (
                ("Como quer ajudar?", "Código, arte, tradução..."),
                ("Experiência", "Conte sobre você"),
                ("Links (GitHub, portfólio)", "Opcional"),
            ),
        ),
        "other": CategoryText(
            "Outro",
            "Qualquer outra coisa",
            "Outro",
            (("Assunto", "Sobre o quê?"), ("Detalhes", "Explique")),
        ),
    },
})


def msg_already_open(channel_id: int | None, language: str = "en") -> str:
    """channel_id is None while their other ticket is still being created."""
    if channel_id is None:
        return t(language, "still_creating")
    return t(language, "already_open", channel_id=channel_id)


def msg_created(channel_mention: str, language: str = "en") -> str:
    return t(language, "ready", channel=channel_mention)


def form_custom_id(category_key: str, language: str) -> str:
    return f"{FORM_BUTTON_PREFIX}{category_key}:{language}"


def parse_form_custom_id(custom_id: str) -> tuple[TicketCategory, str] | None:
    """(category, language) of a form_language_card button, None when it isn't one (or names an unknown one)."""
    if not custom_id.startswith(FORM_BUTTON_PREFIX):
        return None
    key, _, language = custom_id.removeprefix(FORM_BUTTON_PREFIX).partition(":")
    category = CATEGORIES.get(key)
    if category is None or language not in LANGUAGES:
        return None
    return category, language


def form_language_card(category: TicketCategory) -> discord.ui.DesignerView:
    """The panel's ephemeral answer to a member with no or several language roles: their pick opens the form."""
    buttons = [
        discord.ui.Button(label=name, emoji=flag, custom_id=form_custom_id(category.key, code))
        for code, (flag, name) in LANGUAGES.items()
    ]
    return _card(
        discord.ui.TextDisplay(
            f"### 🌐 Pick your language — a form will appear next\n"
            f"-# 🇪🇸 Elige tu idioma: el formulario aparecerá después. "
            f"🇧🇷 Escolha seu idioma: o formulário vem em seguida.\n"
            f"-# {category.emoji} {category.name}"
        ),
        discord.ui.ActionRow(*buttons),
        color=BLURPLE,
    )


# ---- Components V2 cards -----------------------------------------------------------------------
# Buttons carry fixed custom ids and are routed by TicketsCog.on_interaction, so they survive restarts.
# The ticket's button row has a fixed component id, so state changes can swap just that row.


def _card(*items: discord.ui.Item, color: discord.Color) -> discord.ui.DesignerView:
    return discord.ui.DesignerView(discord.ui.Container(*items, color=color), timeout=None)


def _link_row(label: str, emoji: str, url: str) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(discord.ui.Button(label=label, emoji=emoji, url=url))


def _dm_card(
    title: str, body: str, guild: discord.Guild, row: discord.ui.ActionRow | None
) -> discord.ui.DesignerView:
    head = discord.ui.TextDisplay(f"### {title}\n{body}")
    if guild.icon:
        head = discord.ui.Section(head, accessory=discord.ui.Thumbnail(guild.icon.url))
    return _card(head, *([row] if row else []), color=BLURPLE)


def dm_created(
    number: int, category: TicketCategory, channel: discord.TextChannel, language: str
) -> discord.ui.DesignerView:
    return _dm_card(
        f"🎫 Ticket #{number:04d} · {category.emoji} {category.name}",
        t(language, "created"),
        channel.guild,
        _link_row(t(language, "open_ticket"), "🎫", channel.jump_url),
    )


def dm_assigned(
    number: int, channel: discord.TextChannel, by: discord.abc.User, language: str = "en"
) -> discord.ui.DesignerView:
    """Goes to the assigned staff member, in their language."""
    return _dm_card(
        f"📌 Ticket #{number:04d}",
        t(language, "assigned_dm", by=by.display_name),
        channel.guild,
        _link_row(t(language, "open_ticket"), "🎫", channel.jump_url),
    )


def dm_transcript(
    number: int, guild: discord.Guild, url: str, expires_at: datetime | None, language: str
) -> discord.ui.DesignerView:
    keep = t(language, "expires", when=f"<t:{int(expires_at.timestamp())}:R>") if expires_at else t(language, "kept")
    return _dm_card(
        f"📄 Ticket #{number:04d}",
        f"{t(language, 'transcript')} {keep}",
        guild,
        _link_row(t(language, "view_transcript"), "📄", url),
    )


def dm_solved(
    number: int,
    guild: discord.Guild,
    url: str | None,
    expires_at: datetime | None,
    delete_at: datetime,
    language: str,
) -> discord.ui.DesignerView:
    """Replaces dm_transcript when a ticket is closed as solved (TicketsCog.solve_ticket). Only staff can re-open it,
    in the channel. url None: the transcript failed, so no link."""
    body = [t(language, "solved", number=f"{number:04d}")]
    if url and expires_at:
        until = f"<t:{int(expires_at.timestamp())}:f> (<t:{int(expires_at.timestamp())}:R>)"
        body.append(t(language, "solved_until", until=until))
    elif url:
        body.append(t(language, "solved_kept"))
    body.append(t(language, "solved_deletes", deadline=f"<t:{int(delete_at.timestamp())}:R>"))
    row = _link_row(t(language, "view_transcript"), "📄", url) if url else None
    return _dm_card(f"✅ Ticket #{number:04d}", " ".join(body), guild, row)


def deleting_notice(delete_at: datetime, language: str) -> discord.ui.DesignerView:
    """Posted in the channel right before TicketsCog.delete_ticket deletes it."""
    return notice(f"🗑️ {t(language, 'deleting', when=f'<t:{int(delete_at.timestamp())}:R>')}")


def dm_reminder(number: int, channel: discord.TextChannel, language: str) -> discord.ui.DesignerView:
    """The owner hasn't answered their ticket for a while (TicketsCog._remind_owners)."""
    return _dm_card(
        f"⏰ Ticket #{number:04d}: {t(language, 'reminder_title')}",
        t(language, "reminder"),
        channel.guild,
        _link_row(t(language, "open_ticket"), "🎫", channel.jump_url),
    )


def language_row(language: str) -> discord.ui.ActionRow:
    """The welcome card's support-language select; owner and staff can change it."""
    return discord.ui.ActionRow(
        discord.ui.Select(
            custom_id=LANG_SELECT_ID,
            placeholder="🌐 Support language",
            options=[
                discord.SelectOption(label=name, value=code, emoji=flag, default=code == language)
                for code, (flag, name) in LANGUAGES.items()
            ],
        ),
        id=LANG_ROW_ID,
    )


def language_picker(mention: str, languages: list[str]) -> discord.ui.DesignerView:
    """Asks an owner with several language roles, in each of them, which one they want support in."""
    questions = "\n".join(f"🌐 **{t(code, 'pick_language')}**" for code in languages)
    buttons = [
        discord.ui.Button(label=LANGUAGES[code][1], emoji=LANGUAGES[code][0], custom_id=f"{LANG_BUTTON_PREFIX}{code}")
        for code in languages
    ]
    return _card(discord.ui.TextDisplay(f"{mention}\n{questions}"), discord.ui.ActionRow(*buttons), color=BLURPLE)


def language_set(language: str) -> discord.ui.DesignerView:
    """What the picker turns into once someone chose."""
    return notice(f"🌐 {t(language, 'language_set', name=LANGUAGES[language][1])}")


def language_changed(language: str, mention: str) -> discord.ui.DesignerView:
    return notice(f"🌐 {t(language, 'language_changed', name=LANGUAGES[language][1], mention=mention)}")


def control_row(ai_enabled: bool = True, language: str = "en") -> discord.ui.ActionRow:
    """Staff-only AI toggle (doubles as its status), Solved, Summary and Draft reply, plus Close (in the ticket's
    language, the owner uses it too), while the ticket is open. Every state change swaps this whole row, so all five
    buttons stay."""
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="AI support: On" if ai_enabled else "AI support: Off",
            emoji="🤖",
            style=discord.ButtonStyle.success if ai_enabled else discord.ButtonStyle.secondary,
            custom_id=AI_BUTTON_ID,
        ),
        discord.ui.Button(
            label=t(language, "close"), emoji="🔒", style=discord.ButtonStyle.danger, custom_id=CLOSE_BUTTON_ID
        ),
        discord.ui.Button(label="Solved", emoji="✅", style=discord.ButtonStyle.primary, custom_id=SOLVED_BUTTON_ID),
        discord.ui.Button(label="Summary", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=SUMMARY_BUTTON_ID),
        discord.ui.Button(label="Draft reply", emoji="✍️", style=discord.ButtonStyle.secondary, custom_id=DRAFT_BUTTON_ID),
        id=ACTIONS_ID,
    )


def closed_row(url: str | None = None, *, saving: bool = False) -> discord.ui.ActionRow:
    """Transcript slot: "Saving…" while the close archives it, then a link to the page, or Retry if that failed."""
    if saving:
        transcript = discord.ui.Button(label="Saving transcript…", custom_id=TRANSCRIPT_BUTTON_ID, disabled=True)
    elif url:
        transcript = discord.ui.Button(label="Transcript", emoji="📄", url=url)
    else:
        transcript = discord.ui.Button(label="Retry transcript", emoji="📄", custom_id=TRANSCRIPT_BUTTON_ID)
    return discord.ui.ActionRow(
        transcript,
        discord.ui.Button(label="Re-Open", emoji="🔓", style=discord.ButtonStyle.success, custom_id=REOPEN_BUTTON_ID),
        discord.ui.Button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id=DELETE_BUTTON_ID),
        id=ACTIONS_ID,
    )


def request_text(answers: list[tuple[str, str]]) -> str:
    """The intake form, readable at a glance: the first answer is the headline, the rest label + text."""
    if not answers:
        return "-# No details given."
    (first_label, first), *rest = answers
    parts = [f"-# {first_label}\n### {first}"]
    parts += [f"**{label}**\n{value}" for label, value in rest]
    return "\n".join(parts)


def greeting_text(number: int, category: TicketCategory, mention: str, language: str) -> str:
    return (
        f"## 🎫 #{number:04d} · {category.emoji} {category.name}\n"
        f"{t(language, 'greeting', mention=mention)}\n{t(language, 'no_pings')}"
    )


def welcome_view(
    *,
    number: int,
    category: TicketCategory,
    answers: list[tuple[str, str]],
    mention: str,
    language: str,
    ai_enabled: bool = True,
) -> discord.ui.DesignerView:
    """Greeting card, then the request in its own card, then the language select and the button row."""
    greeting = discord.ui.Container(
        discord.ui.TextDisplay(greeting_text(number, category, mention, language)), color=BLURPLE
    )
    request = discord.ui.Container(discord.ui.TextDisplay(request_text(answers)[:3000]), color=REQUEST_GREY)
    return discord.ui.DesignerView(
        greeting, request, language_row(language), control_row(ai_enabled, language), timeout=None
    )


def relanguage_welcome(
    view: discord.ui.DesignerView, *, number: int, category: TicketCategory, mention: str, language: str
) -> discord.ui.DesignerView:
    """The posted welcome card (read back with DesignerView.from_message) with its greeting, select and Close button
    in language."""
    view.children[0].items[0].content = greeting_text(number, category, mention, language)
    if (row := view.get_item(LANG_ROW_ID)) is not None:
        row.children[0].options = language_row(language).children[0].options
    if (actions := view.get_item(ACTIONS_ID)) is not None:
        for button in actions.children:
            if getattr(button, "custom_id", None) == CLOSE_BUTTON_ID:
                button.label = t(language, "close")
    return view


def closed_view(mention: str | None, reason: str | None) -> discord.ui.DesignerView:
    """Only staff can still see the channel once it's closed, so this is English."""
    text = "### 🔒 Ticket closed\n" + (f"Closed by {mention}." if mention else "Closed.")
    if reason:
        text += f"\n**Reason** {reason[:1000]}"
    return discord.ui.DesignerView(
        discord.ui.Container(discord.ui.TextDisplay(text), color=CLOSED_RED), closed_row(saving=True), timeout=None
    )


def reopened_view(mention: str, language: str = "en") -> discord.ui.DesignerView:
    """Public in the ticket, so in its language."""
    return _card(
        discord.ui.TextDisplay(f"### 🔓 {t(language, 'reopened')}\n{t(language, 'reopened_by', mention=mention)}"),
        color=REOPENED_GREEN,
    )


def panel_view() -> discord.ui.DesignerView:
    """The public panel everyone sees: English, plus one short line for Spanish and Portuguese speakers. The form that
    follows a pick is in the member's own language."""
    select = discord.ui.Select(
        custom_id=PANEL_SELECT_ID,
        placeholder="Select a category",
        options=[
            discord.SelectOption(label=c.name, value=c.key, description=c.description, emoji=c.emoji)
            for c in CATEGORIES.values()
        ],
    )
    return _card(
        discord.ui.TextDisplay(
            "## 🎫 Support Tickets\n"
            "Need help? Pick a category below. A short form opens in your language, then a private channel is "
            "created for you.\n"
            "-# 🇪🇸 Elige una categoría: el formulario aparece en tu idioma. "
            "🇧🇷 Escolha uma categoria: o formulário aparece no seu idioma."
        ),
        discord.ui.ActionRow(select),
        discord.ui.TextDisplay("-# Please don’t open duplicate tickets."),
        color=BLURPLE,
    )


def notice(text: str, color: discord.Color = BLURPLE) -> discord.ui.DesignerView:
    return _card(discord.ui.TextDisplay(text), color=color)


class ClosePrompt(discord.ui.DesignerView):
    """Private "close this ticket?" card in the clicker's language; confirming runs on_confirm(interaction). Every close
    saves a transcript."""

    def __init__(self, on_confirm, language: str = "en") -> None:
        close = discord.ui.Button(label=t(language, "close"), emoji="🔒", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label=t(language, "cancel"))
        close.callback = self._confirm
        cancel.callback = self._cancel
        super().__init__(
            discord.ui.Container(
                discord.ui.TextDisplay(f"### 🔒 {t(language, 'close_title')}\n{t(language, 'close_body')}"),
                discord.ui.ActionRow(close, cancel),
                color=CLOSED_RED,
            ),
            timeout=120,
            disable_on_timeout=True,
        )
        self._on_confirm = on_confirm
        self._language = language
        self._close_id = close.custom_id
        self._working = False

    async def _confirm(self, interaction: discord.Interaction) -> None:
        """on_confirm leaves the prompt in its final state; if it raises, the prompt comes back to try again."""
        if self._working:
            return await interaction.response.send_message(already_working(self._language), ephemeral=True)
        self._working = True
        try:
            await interaction.response.edit_message(view=busy(self, self._close_id))
            await self._on_confirm(interaction)
        except Exception:
            await interaction.edit_original_response(view=ClosePrompt(self._on_confirm, self._language))
            await interaction.followup.send(t(self._language, "failed"), ephemeral=True)
            raise  # View.on_error logs it
        finally:
            self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=notice(t(self._language, "cancelled"), discord.Color.dark_grey()))


def transcript_link_view(number: int, url: str, language: str = "en") -> discord.ui.DesignerView:
    return _card(
        discord.ui.TextDisplay(f"📄 {t(language, 'transcript_of', number=f'{number:04d}')}"),
        _link_row(t(language, "view_transcript"), "📄", url),
        color=BLURPLE,
    )


class TicketModal(discord.ui.DesignerModal):
    """A category's intake form, entirely in language; the ticket it opens gets that support language."""

    # Label-wrapped inputs: Discord shows legacy action-row inputs as required even with required=false.
    def __init__(self, category: TicketCategory, language: str = "en") -> None:
        text = category.text(language)
        labels = [
            discord.ui.Label(
                label,
                discord.ui.InputText(
                    placeholder=placeholder,
                    style=discord.InputTextStyle.paragraph if field.paragraph else discord.InputTextStyle.short,
                    required=field.required,
                    max_length=field.max_length,
                ),
            )
            for field, (label, placeholder) in zip(category.fields, text.fields)
        ]
        for field, label in zip(category.fields, labels):
            # py-cord 2.7.x drops required=False in the constructor (`required or self.required`), leaving None.
            label.item.required = field.required
        super().__init__(*labels, title=text.title)
        self.category = category
        self.language = language if language in LANGUAGES else "en"

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog(COG_NAME)
        if cog is None:
            return await interaction.response.send_message(t(self.language, "failed"), ephemeral=True)
        # The request card and the AI read English field labels, whatever language the form was in.
        answers = [
            (label, (item.item.value or "").strip())
            for (label, _), item in zip(self.category.text("en").fields, self.children)
        ]
        await cog.create_ticket(
            interaction,
            self.category,
            [(label, value) for label, value in answers if value],
            language=self.language,
        )


def ticket_log_card(
    ticket,
    *,
    closed_by_id: int | None,
    resolved: bool,
    message_count: int,
    summary,
    url: str | None,
) -> discord.ui.DesignerView:
    """The staff record of a close in ticket_log_channel_id (English): who, what, the AI summary and the transcript."""
    category = CATEGORIES.get(ticket.category)
    opened = f"<t:{int(ticket.created_at.timestamp())}:f>" if ticket.created_at else None
    head = f"### {'✅' if resolved else '📁'} Ticket #{ticket.ticket_id:04d}" + (
        f" · {category.emoji} {category.name}" if category else ""
    )
    info = facts(
        ("Owner", f"<@{ticket.owner_id}>"),
        ("Closed by", f"<@{closed_by_id}>" if closed_by_id else "automatic"),
        ("Language", ticket.language),
        ("Opened", opened),
        ("Messages", message_count),
        ("Solved", "yes" if resolved else "no"),
    )
    body = None
    if summary is not None:
        body = trim(f"**{summary.title}**\n**Problem:** {summary.problem}\n**Resolution:** {summary.resolution}", 3000)
    buttons = [discord.ui.Button(label="Transcript", emoji="📄", url=url)] if url else None
    return card(head, info, body, color=0x2ECC71 if resolved else 0x95A5A6, buttons=buttons)
