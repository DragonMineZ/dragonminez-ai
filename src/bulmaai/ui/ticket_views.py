"""Ticket panel dropdown, intake modals, and the persistent in-ticket buttons (cogs/tickets.py does the work).

Member-facing text is English | Spanish | Portuguese, but Portuguese is left out where it's near-identical to
Spanish (Resumen/Resumo, Versión/Versão...). Staff-only buttons and notices are English. Views are stateless:
each callback looks the ticket up by channel, so they survive bot restarts.
"""

from dataclasses import dataclass
from datetime import datetime

import discord

PANEL_SELECT_ID = "ticket_panel_select"
CLOSE_BUTTON_ID = "ticket_btn_close"
TRANSCRIPT_BUTTON_ID = "ticket_btn_transcript"
REOPEN_BUTTON_ID = "ticket_btn_reopen"
DELETE_BUTTON_ID = "ticket_btn_delete"
COG_NAME = "TicketsCog"

BLURPLE = discord.Color.from_rgb(88, 101, 242)
CLOSED_RED = discord.Color.from_rgb(237, 66, 69)
REOPENED_GREEN = discord.Color.from_rgb(87, 242, 135)


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
                FormField(tri("Availability", "Horario"), tri("Hours per week", "Horas por semana"), required=False),
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


def _dm_embed(title: str, en: str, es: str, pt: str, color: discord.Color, guild: discord.Guild) -> discord.Embed:
    """English paragraph, then Spanish and Portuguese in italics underneath."""
    embed = discord.Embed(title=title, description=f"{en}\n\n*{es}*\n\n*{pt}*", color=color)
    embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
    return embed


def dm_created(number: int, category: TicketCategory, channel: discord.TextChannel) -> tuple[discord.Embed, discord.ui.View]:
    embed = _dm_embed(
        f"🎫 Ticket #{number:04d} · {category.emoji} {category.name}",
        "Your ticket has been created. Staff will answer you there, no need to ping them.",
        "Tu ticket ha sido creado. El staff te responderá allí, no hace falta mencionarlos.",
        "Seu ticket foi criado. A staff vai te responder lá, não precisa marcá-los.",
        BLURPLE,
        channel.guild,
    )
    view = discord.ui.View(discord.ui.Button(label=tri("Open ticket", "Abrir ticket"), emoji="🎫", url=channel.jump_url))
    return embed, view


def dm_transcript(
    number: int, guild: discord.Guild, url: str, expires_at: datetime | None
) -> tuple[discord.Embed, discord.ui.View]:
    if expires_at:
        when = f"<t:{int(expires_at.timestamp())}:R>"
        keep = (f"It expires {when}.", f"Expira {when}.", f"Expira {when}.")
    else:
        keep = ("It is kept permanently.", "Se guarda para siempre.", "Fica guardada para sempre.")
    embed = _dm_embed(
        f"📄 Ticket #{number:04d}",
        f"Your ticket was closed, here is its transcript. {keep[0]}",
        f"Tu ticket fue cerrado, aquí tienes su transcripción. {keep[1]}",
        f"Seu ticket foi fechado, aqui está a transcrição. {keep[2]}",
        BLURPLE,
        guild,
    )
    view = discord.ui.View(discord.ui.Button(label=tri("View transcript", "Ver transcripción"), emoji="📄", url=url))
    return embed, view


def closed_embed(mention: str | None, reason: str | None) -> discord.Embed:
    who = f" by {mention}" if mention else ""
    who_es = f" por {mention}" if mention else ""
    embed = discord.Embed(
        title=tri("🔒 Ticket closed", "Ticket cerrado", "Ticket fechado"),
        description=tri(f"Closed{who}.", f"Cerrado{who_es}.", f"Fechado{who_es}."),
        color=CLOSED_RED,
    )
    if reason:
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
    return embed


def reopened_embed(mention: str) -> discord.Embed:
    return discord.Embed(
        title=tri("🔓 Ticket re-opened", "Ticket reabierto", "Ticket reaberto"),
        description=tri(f"Re-opened by {mention}.", f"Reabierto por {mention}.", f"Reaberto por {mention}."),
        color=REOPENED_GREEN,
    )


def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=tri("🎫 Support Tickets", "Tickets de Soporte", "Tickets de Suporte"), color=BLURPLE
    )
    embed.add_field(
        name="🇬🇧 English",
        value="Need help? Pick a category below and fill in the short form. A private channel will be created for you.",
        inline=False,
    )
    embed.add_field(
        name="🇪🇸 Español",
        value="¿Necesitas ayuda? Elige una categoría abajo y rellena el formulario corto. Se creará un canal privado para ti.",
        inline=False,
    )
    embed.add_field(
        name="🇧🇷 Português",
        value="Precisa de ajuda? Escolha uma categoria abaixo e preencha o formulário curto. Um canal privado será criado para você.",
        inline=False,
    )
    embed.set_footer(
        text=tri("Please don't open duplicate tickets", "No abras tickets duplicados", "Não abra tickets duplicados")
    )
    return embed


def build_ticket_embed(*, number: int, category: TicketCategory, answers: list[tuple[str, str]]) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎫 #{number:04d} · {category.emoji} {category.name}"[:256],
        description=(
            "Thanks for reaching out! A staff member will be with you soon. "
            "Feel free to add more details, screenshots or logs while you wait.\n"
            "*¡Gracias por escribirnos! Un miembro del staff te atenderá pronto. "
            "Puedes añadir más detalles, capturas o logs mientras esperas.*\n"
            "*Obrigado por entrar em contato! Alguém da staff vai te atender em breve. "
            "Pode adicionar mais detalhes, prints ou logs enquanto espera.*\n\n"
            "⚠️ **Please don't ping staff, pinging them here gets you a warn.**\n"
            "⚠️ **No menciones al staff, hacerlo aquí te dará una advertencia.**\n"
            "⚠️ **Não marque a staff, marcar aqui te dá uma advertência.**"
        ),
        color=BLURPLE,
    )
    for label, value in answers:
        embed.add_field(name=label[:256], value=value[:1024], inline=False)
    return embed


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog(COG_NAME)


async def _cog_or_error(interaction: discord.Interaction):
    cog = _cog(interaction)
    if cog is None:
        await interaction.response.send_message(MSG["failed"], ephemeral=True)
    return cog


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
        cog = await _cog_or_error(interaction)
        if cog is None:
            return
        answers = [
            (field.label.split(" | ")[0], (label.item.value or "").strip())
            for field, label in zip(self.category.fields, self.children)
        ]
        await cog.create_ticket(interaction, self.category, [(label, value) for label, value in answers if value])


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id=PANEL_SELECT_ID,
        placeholder=tri("Select a category", "Selecciona una categoría"),
        options=[
            discord.SelectOption(label=c.label, value=c.key, description=c.description, emoji=c.emoji)
            for c in CATEGORIES.values()
        ],
    )
    async def choose(self, select: discord.ui.Select, interaction: discord.Interaction) -> None:
        category = CATEGORIES.get(select.values[0])
        if category is None:
            return await interaction.response.send_message(MSG["failed"], ephemeral=True)
        await interaction.response.send_modal(TicketModal(category))
        try:
            # Re-sending the view clears the option the dropdown still shows as picked.
            await interaction.message.edit(view=TicketPanelView())
        except discord.HTTPException:
            pass


class TicketControlView(discord.ui.View):
    """Close button on the first ticket message while the ticket is open."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=tri("Close", "Cerrar"),
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id=CLOSE_BUTTON_ID,
    )
    async def close(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if cog := await _cog_or_error(interaction):
            await cog.on_close(interaction)


class TicketClosedView(discord.ui.View):
    """Staff buttons on the "ticket closed" embed."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Transcript",
        emoji="📄",
        style=discord.ButtonStyle.secondary,
        custom_id=TRANSCRIPT_BUTTON_ID,
    )
    async def transcript(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if cog := await _cog_or_error(interaction):
            await cog.on_transcript(interaction)

    @discord.ui.button(
        label="Re-Open",
        emoji="🔓",
        style=discord.ButtonStyle.success,
        custom_id=REOPEN_BUTTON_ID,
    )
    async def reopen(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if cog := await _cog_or_error(interaction):
            await cog.on_reopen(interaction)

    @discord.ui.button(
        label="Delete",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id=DELETE_BUTTON_ID,
    )
    async def delete(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if cog := await _cog_or_error(interaction):
            await cog.on_delete(interaction)

