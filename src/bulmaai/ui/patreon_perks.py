"""Claiming a Patreon perk: the button on the ticket panel's Patreon check and the form behind it."""

import discord

from bulmaai.utils.i18n import t, table

PERK_BUTTON_PREFIX = "patreon_perk:"
COG_NAME = "TicketsCog"
MAX_REFERENCES = 3
USERNAME_MAX = 16
DETAILS_MAX = 1000

TEXT = table({
    "en": {
        "button": "Claim a perk",
        "title": "Claim a Patreon perk",
        "perk": "Perk",
        "username": "Minecraft username",
        "details": "What do you want?",
        "details_hint": "Describe it: colors, style, character it's based on…",
        "references": "Reference images (optional)",
        "clothing": "Custom clothing (one time)",
        "hairstyle": "Custom hairstyle (one time)",
        "npc_skin": "RedRibbon Soldier NPC with your skin",
        "not_eligible": "Perk claims are for active patrons on a tier that includes them, with their last payment "
        "through. If that's you, open a Patreon ticket and the team will check.",
        "all_claimed": "You've already claimed every perk: {claims}. The team is on them in your tickets.",
        "already": "You already claimed that perk.",
        "claim_line": "{perk} ({status})",
        "pending": "waiting",
        "accepted": "in progress",
        "delivered": "delivered",
    },
    "es": {
        "button": "Reclamar un beneficio",
        "title": "Reclamar un beneficio de Patreon",
        "perk": "Beneficio",
        "username": "Usuario de Minecraft",
        "details": "¿Qué quieres?",
        "details_hint": "Descríbelo: colores, estilo, personaje en el que se basa…",
        "references": "Imágenes de referencia (opcional)",
        "clothing": "Ropa personalizada (una vez)",
        "hairstyle": "Peinado personalizado (una vez)",
        "npc_skin": "NPC soldado de la Red Ribbon con tu skin",
        "not_eligible": "Los beneficios son para patrocinadores activos en un nivel que los incluya y con su último "
        "pago aprobado. Si es tu caso, abre un ticket de Patreon y el equipo lo revisará.",
        "all_claimed": "Ya reclamaste todos los beneficios: {claims}. El equipo los está trabajando en tus tickets.",
        "already": "Ya reclamaste ese beneficio.",
        "claim_line": "{perk} ({status})",
        "pending": "en espera",
        "accepted": "en progreso",
        "delivered": "entregado",
    },
    "pt": {
        "button": "Resgatar um benefício",
        "title": "Resgatar um benefício do Patreon",
        "perk": "Benefício",
        "username": "Usuário do Minecraft",
        "details": "O que você quer?",
        "details_hint": "Descreva: cores, estilo, personagem em que se baseia…",
        "references": "Imagens de referência (opcional)",
        "clothing": "Roupa personalizada (uma vez)",
        "hairstyle": "Penteado personalizado (uma vez)",
        "npc_skin": "NPC soldado da Red Ribbon com sua skin",
        "not_eligible": "Os benefícios são para apoiadores ativos num nível que os inclua e com o último pagamento "
        "aprovado. Se for o seu caso, abra um ticket do Patreon e a equipe vai verificar.",
        "all_claimed": "Você já resgatou todos os benefícios: {claims}. A equipe está cuidando deles nos seus tickets.",
        "already": "Você já resgatou esse benefício.",
        "claim_line": "{perk} ({status})",
        "pending": "aguardando",
        "accepted": "em andamento",
        "delivered": "entregue",
    },
})


def claim_button(language: str) -> discord.ui.Button:
    return discord.ui.Button(
        label=t(TEXT, "button", language),
        emoji="🎨",
        style=discord.ButtonStyle.success,
        custom_id=f"{PERK_BUTTON_PREFIX}{language}",
    )


def parse_language(custom_id: str) -> str | None:
    return custom_id.removeprefix(PERK_BUTTON_PREFIX) if custom_id.startswith(PERK_BUTTON_PREFIX) else None


def claims_text(claims, language: str) -> str:
    return ", ".join(
        t(TEXT, "claim_line", language, perk=t(TEXT, claim.perk, language), status=t(TEXT, claim.status, language))
        for claim in claims
    )


class PerkClaimModal(discord.ui.DesignerModal):
    """Perk (only the ones they haven't claimed), Minecraft username, what they want, up to 3 references."""

    def __init__(self, perks: list[str], language: str) -> None:
        self.perk_select = discord.ui.Select(
            options=[discord.SelectOption(label=t(TEXT, perk, language), value=perk) for perk in perks],
            min_values=1,
            max_values=1,
        )
        self.username = discord.ui.InputText(style=discord.InputTextStyle.short, min_length=3, max_length=USERNAME_MAX)
        self.details = discord.ui.InputText(
            style=discord.InputTextStyle.paragraph, placeholder=t(TEXT, "details_hint", language), max_length=DETAILS_MAX
        )
        self.references = discord.ui.FileUpload(min_values=0, max_values=MAX_REFERENCES, required=False)
        self.references.required = False  # py-cord drops required=False in some constructors (see TicketModal)
        super().__init__(
            discord.ui.Label(t(TEXT, "perk", language), self.perk_select),
            discord.ui.Label(t(TEXT, "username", language), self.username),
            discord.ui.Label(t(TEXT, "details", language), self.details),
            discord.ui.Label(t(TEXT, "references", language), self.references),
            title=t(TEXT, "title", language),
        )
        self.language = language

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog(COG_NAME)
        if cog is None:
            return await interaction.response.send_message(t(TEXT, "not_eligible", self.language), ephemeral=True)
        await cog.submit_perk_claim(
            interaction,
            perk=(self.perk_select.values or [""])[0],
            minecraft_username=(self.username.value or "").strip(),
            details=(self.details.value or "").strip(),
            references=list(self.references.values or [])[:MAX_REFERENCES],
            language=self.language,
        )
