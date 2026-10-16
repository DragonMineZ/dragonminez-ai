"""Ticket panel, intake modals and the in-ticket Components V2 cards (cogs/tickets.py does the work).

The panel, forms and ephemeral replies are English | Spanish (with some Portuguese). Member-facing messages inside a
ticket and its DMs are in the ticket's support language only (TEXT). Staff-only buttons and notices are English. Buttons are stateless: TicketsCog.on_interaction routes them by custom id and
looks the ticket up by channel, so they survive bot restarts.
"""

from dataclasses import dataclass
from datetime import datetime

import discord

from bulmaai.ui.v2 import ALREADY_WORKING, busy

PANEL_SELECT_ID = "ticket_panel_select"
CLOSE_BUTTON_ID = "ticket_btn_close"
TRANSCRIPT_BUTTON_ID = "ticket_btn_transcript"
REOPEN_BUTTON_ID = "ticket_btn_reopen"
DELETE_BUTTON_ID = "ticket_btn_delete"
AI_BUTTON_ID = "ticket_btn_ai"
SOLVED_BUTTON_ID = "ticket_btn_solved"
# + ticket id; on the owner's "solved" DM, routed by prefix (there's no channel to look the ticket up by).
SOLVED_REOPEN_PREFIX = "ticket_solved_reopen:"
ACTIONS_ID = 800  # the button row on the welcome / closed cards
LANG_SELECT_ID = "ticket_lang_select"
# + language code; the language picker posted in a new ticket whose owner has several language roles.
LANG_BUTTON_PREFIX = "ticket_lang:"
LANG_ROW_ID = 801  # the support-language select on the welcome card
COG_NAME = "TicketsCog"

LANGUAGES = {"en": ("🇬🇧", "English"), "es": ("🇪🇸", "Español"), "pt": ("🇧🇷", "Português")}

# Member-facing ticket messages, each in the ticket's support language.
TEXT: dict[str, dict[str, str]] = {
    "en": {
        "greeting": "Hey {mention}! Staff will be with you soon. Add screenshots or extra details while you wait.",
        "no_pings": "⚠️ **Don't ping staff, it gets you a warn.**",
        "open_ticket": "Open ticket",
        "view_transcript": "View transcript",
        "created": "Your ticket has been created. Staff will answer you there, no need to ping them.",
        "transcript": "Here is the transcript of your ticket.",
        "expires": "It expires {when}.",
        "kept": "It is kept permanently.",
        "solved": "Ticket #{number} was marked as solved by staff and closed.",
        "solved_until": "Your answer is in the transcript; it's available until {until}.",
        "solved_kept": "Your answer is in the transcript; it's kept permanently.",
        "solved_reopen": "Still not solved? Re-open it before {deadline}; after that the ticket is deleted.",
        "reopen": "Re-open ticket",
        "reopened": "Your ticket was re-opened. Staff will answer you there.",
        "reminder_title": "your ticket is waiting for your reply",
        "reminder": "Your ticket got an answer and is waiting for yours. Reply there so we can keep helping you.",
        "pick_language": "Which language do you want support in?",
        "language_set": "Support language: {name}.",
        "language_changed": "Support language changed to {name} by {mention}.",
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
        "solved": "El staff marcó el ticket #{number} como resuelto y lo cerró.",
        "solved_until": "Tu respuesta está en la transcripción; disponible hasta {until}.",
        "solved_kept": "Tu respuesta está en la transcripción; se guarda para siempre.",
        "solved_reopen": "¿Sigue sin resolverse? Reábrelo antes de {deadline}; después el ticket se eliminará.",
        "reopen": "Reabrir ticket",
        "reopened": "Tu ticket fue reabierto. El staff te responderá allí.",
        "reminder_title": "tu ticket espera tu respuesta",
        "reminder": "Tu ticket recibió una respuesta y espera la tuya. Responde allí para que podamos seguir ayudándote.",
        "pick_language": "¿En qué idioma quieres recibir soporte?",
        "language_set": "Idioma de soporte: {name}.",
        "language_changed": "Idioma de soporte cambiado a {name} por {mention}.",
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
        "solved": "A staff marcou o ticket #{number} como resolvido e o fechou.",
        "solved_until": "Sua resposta está na transcrição; disponível até {until}.",
        "solved_kept": "Sua resposta está na transcrição; ela é guardada para sempre.",
        "solved_reopen": "Ainda não foi resolvido? Reabra antes de {deadline}; depois disso o ticket será excluído.",
        "reopen": "Reabrir ticket",
        "reopened": "Seu ticket foi reaberto. A staff vai te responder lá.",
        "reminder_title": "seu ticket está esperando sua resposta",
        "reminder": "Seu ticket recebeu uma resposta e está esperando a sua. Responda lá para continuarmos te ajudando.",
        "pick_language": "Em qual idioma você quer receber suporte?",
        "language_set": "Idioma do suporte: {name}.",
        "language_changed": "Idioma do suporte alterado para {name} por {mention}.",
    },
}


def t(language: str, key: str, **values) -> str:
    """TEXT in the ticket's language (English for an unknown one)."""
    return TEXT.get(language, TEXT["en"])[key].format(**values)

BLURPLE = discord.Color.from_rgb(88, 101, 242)
CLOSED_RED = discord.Color.from_rgb(237, 66, 69)
REOPENED_GREEN = discord.Color.from_rgb(87, 242, 135)
REQUEST_GREY = discord.Color.from_rgb(79, 84, 92)


def tri(en: str, es: str, pt: str | None = None) -> str:
    """Omit pt when Spanish already reads fine for Portuguese speakers."""
    return " | ".join(part for part in (en, es, pt) if part)


OPTIONAL = tri("Optional", "Opcional")


@dataclass(frozen=True, slots=True)
class FormField:
    label: str
    placeholder: str
    paragraph: bool = False
    required: bool = True
    max_length: int = 100


@dataclass(frozen=True, slots=True)
class TicketCategory:
    key: str
    emoji: str
    label: str
    description: str
    modal_title: str
    fallback_slug: str
    ai_reply: bool
    fields: tuple[FormField, ...]

    @property
    def name(self) -> str:
        """English name, for everything inside a ticket."""
        return self.label.split(" | ")[0]


CATEGORIES: dict[str, TicketCategory] = {
    category.key: category
    for category in (
        TicketCategory(
            key="bug",
            emoji="⚙️",
            label=tri("Game-Breaking Bug", "Bug que rompe el juego", "Bug que quebra o jogo"),
            description=tri("Crashes, corrupted worlds", "Cierres, mundos dañados", "Crashes, mundos corrompidos"),
            modal_title=tri("Bug Report", "Reporte de bug"),
            fallback_slug="bug",
            ai_reply=True,
            fields=(
                FormField(tri("Summary", "Resumen"), tri("Crash when...", "Se cierra al...", "Fecha ao...")),
                FormField(
                    tri("What happened?", "¿Qué pasó?", "O que houve?"),
                    tri("Describe it", "Descríbelo"),
                    paragraph=True,
                    max_length=1000,
                ),
                FormField(
                    tri("Steps to reproduce", "Pasos"),
                    "1. ...\n2. ...",
                    paragraph=True,
                    required=False,
                    max_length=800,
                ),
                FormField(tri("Mod & Minecraft version", "Versión"), "DMZ 2.2 / MC 1.20.1"),
                FormField(tri("Other mods", "Otros mods"), OPTIONAL, required=False, max_length=200),
            ),
        ),
        TicketCategory(
            key="contribute",
            emoji="🙌",
            label=tri("Contributing to the Mod", "Contribuir al mod"),
            description=tri("Code, art, translations", "Código, arte, traducciones"),
            modal_title=tri("Contribute", "Contribuir"),
            fallback_slug="contribute",
            ai_reply=False,
            fields=(
                FormField(
                    tri("How to help?", "¿Cómo ayudar?"),
                    tri("Code, art, translation...", "Código, arte, traducción..."),
                ),
                FormField(
                    tri("Experience", "Experiencia"),
                    tri("Tell us about yourself", "Cuéntanos de ti", "Conte sobre você"),
                    paragraph=True,
                    max_length=800,
                ),
                FormField(tri("Links (GitHub, portfolio)", "Enlaces"), OPTIONAL, required=False, max_length=200),
            ),
        ),
        TicketCategory(
            key="other",
            emoji="💠",
            label=tri("Other", "Otro"),
            description=tri("Anything else", "Cualquier otra cosa"),
            modal_title=tri("Other", "Otro"),
            fallback_slug="help",
            ai_reply=True,
            fields=(
                FormField(tri("Subject", "Asunto"), tri("What is it about?", "¿De qué trata?", "Sobre o quê?")),
                FormField(
                    tri("Details", "Detalles"),
                    tri("Explain", "Explica"),
                    paragraph=True,
                    max_length=1000,
                ),
            ),
        ),
    )
}

MSG = {
    "creating": tri("🎫 Creating your ticket…", "Creando tu ticket…", "Criando seu ticket…"),
    "failed": tri(
        "❌ Something went wrong creating your ticket. Please try again or contact staff.",
        "Algo salió mal al crear tu ticket. Inténtalo de nuevo o contacta al staff.",
        "Algo deu errado ao criar seu ticket. Tente novamente ou fale com a staff.",
    ),
    "no_permission": tri("You can't do that here.", "No puedes hacer eso aquí.", "Você não pode fazer isso aqui."),
    "staff_only": "Only staff can do this.",
    "mod_only": "Only moderators and admins can do this.",
    "not_ticket": "This isn't an open ticket channel.",
    "already_closed": "This ticket is already closed.",
    "not_closed": "This ticket isn't closed.",
    "owner_left": "The ticket owner left the server, so it can't be re-opened.",
    "deleting": "🗑️ Deleting this ticket…",
    "delete_failed": "❌ I couldn't delete this channel. Check my Manage Channels permission.",
    "transcript_failed": "❌ I couldn't generate the transcript.",
    "cannot_reopen": tri("This ticket can no longer be re-opened.", "Este ticket ya no se puede reabrir."),
    "not_your_ticket": tri("This isn't your ticket.", "Este ticket no es tuyo."),
    "reopen_gone": tri(
        "This ticket can't be re-opened: its channel is gone or you left the server.",
        "Este ticket no se puede reabrir: su canal ya no existe o saliste del servidor.",
    ),
    "protected_target": "You can't remove the ticket owner or staff.",
    "done": "Done.",
}


def msg_already_open(channel_id: int | None) -> str:
    """channel_id is None while their other ticket is still being created."""
    if channel_id is None:
        return tri(
            "Your ticket is still being created. Please wait a moment.",
            "Tu ticket todavía se está creando. Espera un momento.",
            "Seu ticket ainda está sendo criado. Aguarde um momento.",
        )
    return tri(
        f"You already have an open ticket: <#{channel_id}>. Please continue there.",
        f"Ya tienes un ticket abierto: <#{channel_id}>. Continúa allí.",
        f"Você já tem um ticket aberto: <#{channel_id}>. Continue nele.",
    )


def msg_created(channel_mention: str) -> str:
    return f"✅ {tri('Your ticket is ready', 'Tu ticket está listo', 'Seu ticket está pronto')}: {channel_mention}"


# ---- Components V2 cards -----------------------------------------------------------------------
# Buttons carry fixed custom ids and are routed by TicketsCog.on_interaction, so they survive restarts.
# The ticket's button row has a fixed component id, so state changes can swap just that row.


def _card(*items: discord.ui.Item, color: discord.Color) -> discord.ui.DesignerView:
    return discord.ui.DesignerView(discord.ui.Container(*items, color=color), timeout=None)


def _link_row(label: str, emoji: str, url: str) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(discord.ui.Button(label=label, emoji=emoji, url=url))


def _dm_card(title: str, body: str, guild: discord.Guild, row: discord.ui.ActionRow) -> discord.ui.DesignerView:
    head = discord.ui.TextDisplay(f"### {title}\n{body}")
    if guild.icon:
        head = discord.ui.Section(head, accessory=discord.ui.Thumbnail(guild.icon.url))
    return _card(head, row, color=BLURPLE)


def dm_created(
    number: int, category: TicketCategory, channel: discord.TextChannel, language: str
) -> discord.ui.DesignerView:
    return _dm_card(
        f"🎫 Ticket #{number:04d} · {category.emoji} {category.name}",
        t(language, "created"),
        channel.guild,
        _link_row(t(language, "open_ticket"), "🎫", channel.jump_url),
    )


def dm_assigned(number: int, channel: discord.TextChannel, by: discord.abc.User) -> discord.ui.DesignerView:
    """Goes to a staff member, so English."""
    return _dm_card(
        f"📌 Ticket #{number:04d}",
        f"{by.display_name} assigned this ticket to you. Please take care of it.",
        channel.guild,
        _link_row("Open ticket", "🎫", channel.jump_url),
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
    ticket_id: int,
    language: str,
) -> discord.ui.DesignerView:
    """Replaces dm_transcript when staff close a ticket as solved; its Re-open button is the owner's only way back.
    url None: the transcript failed, so only the re-open part."""
    body = [t(language, "solved", number=f"{number:04d}")]
    if url and expires_at:
        until = f"<t:{int(expires_at.timestamp())}:f> (<t:{int(expires_at.timestamp())}:R>)"
        body.append(t(language, "solved_until", until=until))
    elif url:
        body.append(t(language, "solved_kept"))
    body.append(t(language, "solved_reopen", deadline=f"<t:{int(delete_at.timestamp())}:R>"))
    buttons = [discord.ui.Button(label=t(language, "view_transcript"), emoji="📄", url=url)] if url else []
    buttons.append(
        discord.ui.Button(
            label=t(language, "reopen"),
            emoji="🔓",
            style=discord.ButtonStyle.success,
            custom_id=f"{SOLVED_REOPEN_PREFIX}{ticket_id}",
        )
    )
    return _dm_card(f"✅ Ticket #{number:04d}", " ".join(body), guild, discord.ui.ActionRow(*buttons))


def dm_reopened(number: int, channel: discord.TextChannel, language: str) -> discord.ui.DesignerView:
    """What the solved DM turns into once its Re-open button worked."""
    return _dm_card(
        f"🔓 Ticket #{number:04d}",
        t(language, "reopened"),
        channel.guild,
        _link_row(t(language, "open_ticket"), "🎫", channel.jump_url),
    )


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


def control_row(ai_enabled: bool = True) -> discord.ui.ActionRow:
    """Staff-only AI toggle (doubles as its status) and Solved, plus Close, while the ticket is open."""
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="AI support: On" if ai_enabled else "AI support: Off",
            emoji="🤖",
            style=discord.ButtonStyle.success if ai_enabled else discord.ButtonStyle.secondary,
            custom_id=AI_BUTTON_ID,
        ),
        discord.ui.Button(
            label=tri("Close", "Cerrar"), emoji="🔒", style=discord.ButtonStyle.danger, custom_id=CLOSE_BUTTON_ID
        ),
        discord.ui.Button(label="Solved", emoji="✅", style=discord.ButtonStyle.primary, custom_id=SOLVED_BUTTON_ID),
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
    return discord.ui.DesignerView(greeting, request, language_row(language), control_row(ai_enabled), timeout=None)


def relanguage_welcome(
    view: discord.ui.DesignerView, *, number: int, category: TicketCategory, mention: str, language: str
) -> discord.ui.DesignerView:
    """The posted welcome card (read back with DesignerView.from_message) with its greeting and select in language."""
    view.children[0].items[0].content = greeting_text(number, category, mention, language)
    if (row := view.get_item(LANG_ROW_ID)) is not None:
        row.children[0].options = language_row(language).children[0].options
    return view


def closed_view(mention: str | None, reason: str | None) -> discord.ui.DesignerView:
    """Only staff can still see the channel once it's closed, so this is English."""
    text = "### 🔒 Ticket closed\n" + (f"Closed by {mention}." if mention else "Closed.")
    if reason:
        text += f"\n**Reason** {reason[:1000]}"
    return discord.ui.DesignerView(
        discord.ui.Container(discord.ui.TextDisplay(text), color=CLOSED_RED), closed_row(saving=True), timeout=None
    )


def reopened_view(mention: str) -> discord.ui.DesignerView:
    return _card(
        discord.ui.TextDisplay(f"### 🔓 {tri('Ticket re-opened', 'Ticket reabierto')}\nRe-opened by {mention}."),
        color=REOPENED_GREEN,
    )


def panel_view() -> discord.ui.DesignerView:
    select = discord.ui.Select(
        custom_id=PANEL_SELECT_ID,
        placeholder=tri("Select a category", "Selecciona una categoría"),
        options=[
            discord.SelectOption(label=c.label, value=c.key, description=c.description, emoji=c.emoji)
            for c in CATEGORIES.values()
        ],
    )
    return _card(
        discord.ui.TextDisplay(
            f"## 🎫 {tri('Support Tickets', 'Tickets de Soporte')}\n"
            "Need help? Pick a category below and fill in the short form. A private channel will be created for you.\n"
            "-# ¿Necesitas ayuda? Elige una categoría abajo y rellena el formulario. Se creará un canal privado para ti."
        ),
        discord.ui.ActionRow(select),
        discord.ui.TextDisplay(f"-# {tri('Please don’t open duplicate tickets', 'No abras tickets duplicados')}"),
        color=BLURPLE,
    )


def notice(text: str, color: discord.Color = BLURPLE) -> discord.ui.DesignerView:
    return _card(discord.ui.TextDisplay(text), color=color)


class ClosePrompt(discord.ui.DesignerView):
    """Private "close this ticket?" card; confirming runs on_confirm(interaction). Every close saves a transcript."""

    def __init__(self, on_confirm) -> None:
        close = discord.ui.Button(label=tri("Close", "Cerrar"), emoji="🔒", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label=tri("Cancel", "Cancelar"))
        close.callback = self._confirm
        cancel.callback = self._cancel
        super().__init__(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "### 🔒 Close this ticket?\n"
                    "Once closed, only staff can see this channel. A transcript of the conversation is shown here "
                    "and sent by DM.\n"
                    "-# ¿Cerrar este ticket? Después solo el staff podrá ver el canal. "
                    "La transcripción aparece aquí y te llega por MD."
                ),
                discord.ui.ActionRow(close, cancel),
                color=CLOSED_RED,
            ),
            timeout=120,
            disable_on_timeout=True,
        )
        self._on_confirm = on_confirm
        self._close_id = close.custom_id
        self._working = False

    async def _confirm(self, interaction: discord.Interaction) -> None:
        """on_confirm leaves the prompt in its final state; if it raises, the prompt comes back to try again."""
        if self._working:
            return await interaction.response.send_message(ALREADY_WORKING, ephemeral=True)
        self._working = True
        try:
            await interaction.response.edit_message(view=busy(self, self._close_id))
            await self._on_confirm(interaction)
        except Exception:
            await interaction.edit_original_response(view=ClosePrompt(self._on_confirm))
            await interaction.followup.send(MSG["failed"], ephemeral=True)
            raise  # View.on_error logs it
        finally:
            self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=notice(tri("Cancelled.", "Cancelado."), discord.Color.dark_grey()))


def transcript_link_view(number: int, url: str) -> discord.ui.DesignerView:
    return _card(
        discord.ui.TextDisplay(f"📄 {tri(f'Transcript of ticket #{number:04d}', f'Transcripción del ticket #{number:04d}')}"),
        _link_row(tri("View transcript", "Ver transcripción"), "📄", url),
        color=BLURPLE,
    )


class TicketModal(discord.ui.DesignerModal):
    # Label-wrapped inputs: Discord shows legacy action-row inputs as required even with required=false.
    def __init__(self, category: TicketCategory) -> None:
        labels = [
            discord.ui.Label(
                field.label,
                discord.ui.InputText(
                    placeholder=field.placeholder,
                    style=discord.InputTextStyle.paragraph if field.paragraph else discord.InputTextStyle.short,
                    required=field.required,
                    max_length=field.max_length,
                ),
            )
            for field in category.fields
        ]
        for field, label in zip(category.fields, labels):
            # py-cord 2.7.x drops required=False in the constructor (`required or self.required`), leaving None.
            label.item.required = field.required
        super().__init__(*labels, title=category.modal_title)
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog(COG_NAME)
        if cog is None:
            return await interaction.response.send_message(MSG["failed"], ephemeral=True)
        answers = [
            (field.label.split(" | ")[0], (label.item.value or "").strip())
            for field, label in zip(self.category.fields, self.children)
        ]
        await cog.create_ticket(interaction, self.category, [(label, value) for label, value in answers if value])
