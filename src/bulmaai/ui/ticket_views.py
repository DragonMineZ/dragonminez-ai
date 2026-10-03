"""Ticket panel dropdown, intake modals, and the persistent in-ticket buttons (cogs/tickets.py does the work).

The public panel (dropdown, forms) is English | Spanish | Portuguese because the visitor's language isn't known
yet; everything inside a ticket is English. Views are stateless: each callback
looks the ticket up by channel, so they survive bot restarts.
"""

from dataclasses import dataclass
from datetime import datetime

import discord

PANEL_SELECT_ID = "ticket_panel_select"
CLAIM_BUTTON_ID = "ticket_btn_claim"
CLOSE_BUTTON_ID = "ticket_btn_close"
REOPEN_BUTTON_ID = "ticket_btn_reopen"
DELETE_BUTTON_ID = "ticket_btn_delete"
COG_NAME = "TicketsCog"

BLURPLE = discord.Color.from_rgb(88, 101, 242)


def tri(en: str, es: str, pt: str) -> str:
    return f"{en} | {es} | {pt}"


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
            modal_title=tri("Bug Report", "Reporte de bug", "Relato de bug"),
            fallback_slug="bug",
            ai_reply=True,
            fields=(
                FormField(
                    tri("Summary", "Resumen", "Resumo"),
                    tri("Crash when...", "Se cierra al...", "Fecha ao..."),
                ),
                FormField(
                    tri("What happened?", "¿Qué pasó?", "O que houve?"),
                    tri("Describe it", "Descríbelo", "Descreva"),
                    paragraph=True,
                    max_length=1000,
                ),
                FormField(
                    tri("Steps to reproduce", "Pasos", "Passos"),
                    tri("1. ... 2. ...", "1. ... 2. ...", "1. ... 2. ..."),
                    paragraph=True,
                    required=False,
                    max_length=800,
                ),
                FormField(
                    tri("Mod & Minecraft version", "Versión", "Versão"),
                    tri("DMZ 2.2 / MC 1.20.1", "DMZ 2.2 / MC 1.20.1", "DMZ 2.2 / MC 1.20.1"),
                    required=False,
                ),
                FormField(
                    tri("Other mods", "Otros mods", "Outros mods"),
                    tri("Optional", "Opcional", "Opcional"),
                    required=False,
                    max_length=200,
                ),
            ),
        ),
        TicketCategory(
            key="contribute",
            emoji="🙌",
            label=tri("Contributing to the Mod", "Contribuir al mod", "Contribuir com o mod"),
            description=tri("Code, art, translations", "Código, arte, traducciones", "Código, arte, traduções"),
            modal_title=tri("Contribute", "Contribuir", "Contribuir"),
            fallback_slug="contribute",
            ai_reply=False,
            fields=(
                FormField(
                    tri("How to help?", "¿Cómo ayudar?", "Como ajudar?"),
                    tri("Code, art, translation...", "Código, arte, traducción...", "Código, arte, tradução..."),
                ),
                FormField(
                    tri("Experience", "Experiencia", "Experiência"),
                    tri("Tell us about yourself", "Cuéntanos de ti", "Conte sobre você"),
                    paragraph=True,
                    max_length=800,
                ),
                FormField(
                    tri("Links (GitHub, portfolio)", "Enlaces", "Links"),
                    tri("Optional", "Opcional", "Opcional"),
                    required=False,
                    max_length=200,
                ),
                FormField(
                    tri("Availability", "Horario", "Horário"),
                    tri("Hours per week", "Horas por semana", "Horas por semana"),
                    required=False,
                ),
            ),
        ),
        TicketCategory(
            key="other",
            emoji="💠",
            label=tri("Other", "Otro", "Outro"),
            description=tri("Anything else", "Cualquier otra cosa", "Qualquer outra coisa"),
            modal_title=tri("Other", "Otro", "Outro"),
            fallback_slug="help",
            ai_reply=True,
            fields=(
                FormField(tri("Subject", "Asunto", "Assunto"), tri("What is it about?", "¿De qué trata?", "Sobre o quê?")),
                FormField(
                    tri("Details", "Detalles", "Detalhes"),
                    tri("Explain", "Explica", "Explique"),
                    paragraph=True,
                    max_length=1000,
                ),
            ),
        ),
    )
}

MSG = {
    "creating": "🎫 Creating your ticket…",
    "failed": tri(
        "❌ Something went wrong creating your ticket. Please try again or contact staff.",
        "❌ Algo salió mal al crear tu ticket. Inténtalo de nuevo o contacta al staff.",
        "❌ Algo deu errado ao criar seu ticket. Tente novamente ou fale com a staff.",
    ),
    "no_permission": tri("You can't do that here.", "No puedes hacer eso aquí.", "Você não pode fazer isso aqui."),
    "staff_only": "Only staff can do this.",
    "mod_only": "Only moderators and admins can do this.",
    "not_ticket": "This isn't an open ticket channel.",
    "already_closed": "This ticket is already closed.",
    "not_closed": "This ticket isn't closed.",
    "owner_left": "The ticket owner left the server, so it can't be re-opened.",
    "deleting": "🗑️ Deleting this ticket…",
    "archive_failed": (
        "❌ I couldn't save the transcript, so the channel was kept. Check the archive channel and my permissions."
    ),
    "transcript_failed": "❌ I couldn't generate the transcript.",
    "protected_target": "You can't remove the ticket owner or staff.",
    "done": "Done.",
}


def msg_limit(count: int) -> str:
    return tri(
        f"You already have {count} open tickets. Please use one of them.",
        f"Ya tienes {count} tickets abiertos. Usa uno de ellos.",
        f"Você já tem {count} tickets abertos. Use um deles.",
    )


def msg_created(channel_mention: str) -> str:
    return f"✅ Your ticket is ready: {channel_mention}"


def msg_dm_created(number: int, url: str) -> str:
    return f"🎫 Your ticket #{number:04d} has been created: {url}"


def msg_dm_transcript(number: int, url: str, expires_at: datetime | None) -> str:
    keep = f"It expires <t:{int(expires_at.timestamp())}:R>." if expires_at else "It is kept permanently."
    return f"🎫 Your ticket #{number:04d} was closed. Here is the transcript: {url}\n{keep}"


def msg_claimed_by(mention: str) -> str:
    return f"Already claimed by {mention}."


def notice_claimed(mention: str) -> str:
    return f"🙋 {mention} claimed this ticket."


def notice_released(mention: str) -> str:
    return f"🙋 {mention} released this ticket."


def notice_closed(mention: str | None, reason: str | None) -> str:
    text = f"🔒 Ticket closed by {mention}." if mention else "🔒 Ticket closed."
    return f"{text}\n{reason}" if reason else text


def notice_reopened(mention: str) -> str:
    return f"🔓 Ticket re-opened by {mention}."


def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=tri("🎫 Support Tickets", "🎫 Tickets de Soporte", "🎫 Tickets de Suporte"),
        color=BLURPLE,
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


def build_ticket_embed(
    *,
    number: int,
    category: TicketCategory,
    answers: list[tuple[str, str]],
    summary: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎫 #{number:04d} · {category.emoji} {category.name}"[:256],
        description=(
            "Thanks for reaching out! A staff member will be with you soon. "
            "Feel free to add more details, screenshots or logs while you wait."
        ),
        color=BLURPLE,
    )
    for label, value in answers:
        embed.add_field(name=label[:256], value=value[:1024], inline=False)
    if summary:
        embed.add_field(name="🤖 AI summary", value=summary[:1024], inline=False)
    return embed


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog(COG_NAME)


async def _cog_or_error(interaction: discord.Interaction):
    cog = _cog(interaction)
    if cog is None:
        await interaction.response.send_message(MSG["failed"], ephemeral=True)
    return cog


class TicketModal(discord.ui.Modal):
    def __init__(self, category: TicketCategory) -> None:
        super().__init__(
            *(
                discord.ui.InputText(
                    label=field.label,
                    placeholder=field.placeholder,
                    style=discord.InputTextStyle.paragraph if field.paragraph else discord.InputTextStyle.short,
                    required=field.required,
                    max_length=field.max_length,
                )
                for field in category.fields
            ),
            title=category.modal_title,
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _cog_or_error(interaction)
        if cog is None:
            return
        answers = [
            (field.label.split(" | ")[0], (child.value or "").strip())
            for field, child in zip(self.category.fields, self.children)
        ]
        await cog.create_ticket(interaction, self.category, [(label, value) for label, value in answers if value])


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id=PANEL_SELECT_ID,
        placeholder=tri("Select a category", "Selecciona una categoría", "Selecione uma categoria"),
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
    """Buttons on the first ticket message while the ticket is open."""

    def __init__(self, *, claimed: bool = False) -> None:
        super().__init__(timeout=None)
        if claimed:
            self.claim.label = "Release"
            self.claim.style = discord.ButtonStyle.secondary

    @discord.ui.button(
        label="Claim",
        emoji="🙋",
        style=discord.ButtonStyle.primary,
        custom_id=CLAIM_BUTTON_ID,
    )
    async def claim(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if cog := await _cog_or_error(interaction):
            await cog.on_claim(interaction)

    @discord.ui.button(
        label="Close",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id=CLOSE_BUTTON_ID,
    )
    async def close(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if cog := await _cog_or_error(interaction):
            await cog.on_close(interaction)


class TicketModeratorView(discord.ui.View):
    """Replaces the control buttons once a ticket is closed."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

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
