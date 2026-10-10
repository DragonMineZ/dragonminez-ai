"""Ticket panel, intake modals and the in-ticket Components V2 cards (cogs/tickets.py does the work).

Member-facing text is English | Spanish (the panel dropdown and forms still carry some Portuguese). Staff-only
buttons and notices are English. Buttons are stateless: TicketsCog.on_interaction routes them by custom id and
looks the ticket up by channel, so they survive bot restarts.
"""

from dataclasses import dataclass
from datetime import datetime

import discord

PANEL_SELECT_ID = "ticket_panel_select"
CLOSE_BUTTON_ID = "ticket_btn_close"
TRANSCRIPT_BUTTON_ID = "ticket_btn_transcript"
REOPEN_BUTTON_ID = "ticket_btn_reopen"
DELETE_BUTTON_ID = "ticket_btn_delete"
AI_BUTTON_ID = "ticket_btn_ai"
ACTIONS_ID = 800  # the button row on the welcome / closed cards
COG_NAME = "TicketsCog"

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
    "protected_target": "You can't remove the ticket owner or staff.",
    "done": "Done.",
    "no_staff_pings": (
        "please don't ping staff in tickets, they'll get to you. You have been warned.\n"
        "*No menciones al staff en los tickets, te atenderán pronto. Has recibido una advertencia.*\n"
        "*Não marque a staff nos tickets, eles vão te atender. Você recebeu uma advertência.*"
    ),
}


def msg_limit(count: int) -> str:
    return tri(
        f"You already have {count} open tickets. Please use one of them.",
        f"Ya tienes {count} tickets abiertos. Usa uno de ellos.",
        f"Você já tem {count} tickets abertos. Use um deles.",
    )


def msg_created(channel_mention: str) -> str:
    return f"✅ {tri('Your ticket is ready', 'Tu ticket está listo', 'Seu ticket está pronto')}: {channel_mention}"


def msg_transcript_ready(url: str) -> str:
    return f"📄 Transcript saved: {url}"


# ---- Components V2 cards -----------------------------------------------------------------------
# Buttons carry fixed custom ids and are routed by TicketsCog.on_interaction, so they survive restarts.
# The ticket's button row has a fixed component id, so state changes can swap just that row.


def _card(*items: discord.ui.Item, color: discord.Color) -> discord.ui.DesignerView:
    return discord.ui.DesignerView(discord.ui.Container(*items, color=color), timeout=None)


def _link_row(label: str, emoji: str, url: str) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(discord.ui.Button(label=label, emoji=emoji, url=url))


def _dm_card(title: str, en: str, es: str, guild: discord.Guild, row: discord.ui.ActionRow) -> discord.ui.DesignerView:
    """English paragraph, Spanish underneath as subtext."""
    head = discord.ui.TextDisplay(f"### {title}\n{en}\n-# {es}")
    if guild.icon:
        head = discord.ui.Section(head, accessory=discord.ui.Thumbnail(guild.icon.url))
    return _card(head, row, color=BLURPLE)


def dm_created(number: int, category: TicketCategory, channel: discord.TextChannel) -> discord.ui.DesignerView:
    return _dm_card(
        f"🎫 Ticket #{number:04d} · {category.emoji} {category.name}",
        "Your ticket has been created. Staff will answer you there, no need to ping them.",
        "Tu ticket ha sido creado. El staff te responderá allí, no hace falta mencionarlos.",
        channel.guild,
        _link_row(tri("Open ticket", "Abrir ticket"), "🎫", channel.jump_url),
    )


def dm_assigned(number: int, channel: discord.TextChannel, by: discord.abc.User) -> discord.ui.DesignerView:
    return _dm_card(
        f"📌 Ticket #{number:04d}",
        f"{by.display_name} assigned this ticket to you. Please take care of it.",
        f"{by.display_name} te asignó este ticket. Por favor, encárgate de él.",
        channel.guild,
        _link_row(tri("Open ticket", "Abrir ticket"), "🎫", channel.jump_url),
    )


def dm_transcript(number: int, guild: discord.Guild, url: str, expires_at: datetime | None) -> discord.ui.DesignerView:
    if expires_at:
        when = f"<t:{int(expires_at.timestamp())}:R>"
        keep = (f"It expires {when}.", f"Expira {when}.")
    else:
        keep = ("It is kept permanently.", "Se guarda para siempre.")
    return _dm_card(
        f"📄 Ticket #{number:04d}",
        f"Here is the transcript of your ticket. {keep[0]}",
        f"Aquí tienes la transcripción de tu ticket. {keep[1]}",
        guild,
        _link_row(tri("View transcript", "Ver transcripción"), "📄", url),
    )


def control_row(ai_enabled: bool = True) -> discord.ui.ActionRow:
    """Staff-only AI toggle (doubles as its status) and Close, while the ticket is open."""
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
        id=ACTIONS_ID,
    )


def closed_row(*, transcript: bool = True) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(
        discord.ui.Button(label="Transcript", emoji="📄", custom_id=TRANSCRIPT_BUTTON_ID, disabled=not transcript),
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


def welcome_view(
    *, number: int, category: TicketCategory, answers: list[tuple[str, str]], mention: str, ai_enabled: bool = True
) -> discord.ui.DesignerView:
    """Greeting card, then the request in its own card, then the button row."""
    greeting = discord.ui.Container(
        discord.ui.TextDisplay(
            f"## 🎫 #{number:04d} · {category.emoji} {category.name}\n"
            f"Hey {mention}! Staff will be with you soon. Add screenshots or extra details while you wait.\n"
            "-# ¡Hola! El staff te atenderá pronto. Puedes añadir capturas o más detalles mientras esperas.\n"
            "⚠️ **Don't ping staff, it gets you a warn.** · *No menciones al staff, te dará una advertencia.*"
        ),
        color=BLURPLE,
    )
    request = discord.ui.Container(discord.ui.TextDisplay(request_text(answers)[:3000]), color=REQUEST_GREY)
    return discord.ui.DesignerView(greeting, request, control_row(ai_enabled), timeout=None)


def closed_view(mention: str | None, reason: str | None) -> discord.ui.DesignerView:
    """Only staff can still see the channel once it's closed, so this is English."""
    text = "### 🔒 Ticket closed\n" + (f"Closed by {mention}." if mention else "Closed.")
    if reason:
        text += f"\n**Reason** {reason[:1000]}"
    return discord.ui.DesignerView(discord.ui.Container(discord.ui.TextDisplay(text), color=CLOSED_RED), closed_row(), timeout=None)


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
    """Private "close this ticket?" card; the choice runs on_choice(interaction, transcript)."""

    def __init__(self, on_choice) -> None:
        with_transcript = discord.ui.Button(
            label=tri("Close + transcript", "Cerrar + transcripción"), emoji="📄", style=discord.ButtonStyle.primary
        )
        close_only = discord.ui.Button(label=tri("Just close", "Solo cerrar"), emoji="🔒", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label=tri("Cancel", "Cancelar"))
        with_transcript.callback = lambda interaction: self._pick(interaction, True)
        close_only.callback = lambda interaction: self._pick(interaction, False)
        cancel.callback = self._cancel
        super().__init__(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "### 🔒 Close this ticket?\n"
                    "Once closed, only staff can see this channel. Want a transcript of the conversation? "
                    "It's sent by DM and shown here.\n"
                    "-# ¿Cerrar este ticket? Después solo el staff podrá ver el canal. "
                    "¿Quieres la transcripción? Te llega por MD y aparece aquí."
                ),
                discord.ui.ActionRow(with_transcript, close_only, cancel),
                color=CLOSED_RED,
            ),
            timeout=120,
            disable_on_timeout=True,
        )
        self._on_choice = on_choice

    async def _pick(self, interaction: discord.Interaction, transcript: bool) -> None:
        self.stop()
        await interaction.response.edit_message(view=notice(f"⏳ {tri('Closing…', 'Cerrando…')}", discord.Color.dark_grey()))
        await self._on_choice(interaction, transcript)

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
