import re

import discord

from bulmaai.utils.i18n import t, table

MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

PATREON_WELCOME_VERIFY_CUSTOM_ID = "patreon_welcome:verify"

# The modal and the confirm card are shown to one member, in their language.
TEXT = table({
    "en": {
        "title": "DragonMineZ Beta Access",
        "username": "Minecraft username",
        "confirm": "### 🔁 Update your username?\nYou're already whitelisted, but we can update your username: "
        "`{old}` will be changed to `{new}`. Continue?",
        "yes": "Yes",
        "no": "No",
        "only_author": "Only the command author can use these buttons.",
        "submitted": "This username update was already submitted.",
        "updating": "⏳ Updating `{old}` to `{new}`…",
        "cancelled": "Username update cancelled.",
    },
    "es": {
        "title": "Acceso beta de DragonMineZ",
        "username": "Nombre de Minecraft",
        "confirm": "### 🔁 ¿Actualizar tu nombre?\nYa estás en la whitelist, pero podemos actualizar tu nombre: "
        "`{old}` va a cambiar a `{new}`. ¿Seguimos?",
        "yes": "Sí",
        "no": "No",
        "only_author": "Solo quien usó el comando puede usar estos botones.",
        "submitted": "Este cambio de nombre ya se envió.",
        "updating": "⏳ Cambiando `{old}` a `{new}`…",
        "cancelled": "Cambio de nombre cancelado.",
    },
    "pt": {
        "title": "Acesso beta do DragonMineZ",
        "username": "Nome de Minecraft",
        "confirm": "### 🔁 Atualizar seu nome?\nVocê já está na whitelist, mas podemos atualizar seu nome: "
        "`{old}` vai mudar para `{new}`. Continuar?",
        "yes": "Sim",
        "no": "Não",
        "only_author": "Só quem usou o comando pode usar estes botões.",
        "submitted": "Esta troca de nome já foi enviada.",
        "updating": "⏳ Mudando `{old}` para `{new}`…",
        "cancelled": "Troca de nome cancelada.",
    },
})


async def _edit_interaction_message(interaction: discord.Interaction, **kwargs) -> None:
    edit_original_response = getattr(interaction, "edit_original_response", None)
    if edit_original_response is not None:
        try:
            await edit_original_response(**kwargs)
            return
        except discord.HTTPException:
            pass

    message = getattr(interaction, "message", None)
    if message is not None:
        try:
            await message.edit(**kwargs)
        except discord.HTTPException:
            pass


class BetaAccessUsernameModal(discord.ui.Modal):
    """Asks for a Minecraft username, then hands off to the whitelist flow."""

    def __init__(self, *, on_submit, language: str = "en"):
        super().__init__(title=t(TEXT, "title", language))
        self.on_submit_callback = on_submit  # async (interaction, username) -> None
        self.username = discord.ui.InputText(
            label=t(TEXT, "username", language),
            placeholder="e.g. Bruno_123",
            min_length=3,
            max_length=16,
            required=True,
        )
        self.add_item(self.username)

    async def callback(self, interaction: discord.Interaction):
        await self.on_submit_callback(interaction, (self.username.value or "").strip())


def welcome_buttons(*, downloads_channel_url: str | None = None) -> list[discord.ui.Button]:
    """Quick-start buttons on the Patreon welcome DM; the verify click is routed by its custom id."""
    buttons = [
        discord.ui.Button(
            label="Verify & Get Beta Access", style=discord.ButtonStyle.success, custom_id=PATREON_WELCOME_VERIFY_CUSTOM_ID
        )
    ]
    if downloads_channel_url:
        buttons.append(discord.ui.Button(label="Open Downloads Channel", url=downloads_channel_url))
    return buttons


class UsernameUpdateConfirmView(discord.ui.DesignerView):
    """"Update your whitelisted username?" card with Yes/No; only the requester can answer."""

    def __init__(self, *, requester_id: int, old_nickname: str, new_nickname: str, on_confirm, language: str = "en"):
        self.language = language
        self.requester_id = requester_id
        self.old_nickname = old_nickname
        self.new_nickname = new_nickname
        self.on_confirm = on_confirm  # async (interaction) -> None
        self._submitted = False
        yes = discord.ui.Button(label=t(TEXT, "yes", language), style=discord.ButtonStyle.success)
        no = discord.ui.Button(label=t(TEXT, "no", language), style=discord.ButtonStyle.danger)
        yes.callback, no.callback = self._yes, self._no
        super().__init__(
            discord.ui.Container(
                discord.ui.TextDisplay(t(TEXT, "confirm", language, old=old_nickname, new=new_nickname)),
                discord.ui.ActionRow(yes, no),
                color=discord.Colour.from_rgb(255, 85, 0),
            ),
            timeout=300,
        )

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(t(TEXT, "only_author", self.language), ephemeral=True)
            return False
        if self._submitted:
            await interaction.response.send_message(t(TEXT, "submitted", self.language), ephemeral=True)
            return False
        return True

    async def _yes(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        self._submitted = True
        await interaction.response.defer()
        await _edit_interaction_message(interaction, view=_status(t(TEXT, "updating", self.language, old=self.old_nickname, new=self.new_nickname)))
        try:
            await self.on_confirm(interaction)
        except Exception:
            self._submitted = False
            raise

    async def _no(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        self._submitted = True
        await interaction.response.defer()
        await _edit_interaction_message(interaction, view=_status(t(TEXT, "cancelled", self.language)))


def _status(text: str) -> discord.ui.DesignerView:
    return discord.ui.DesignerView(discord.ui.Container(discord.ui.TextDisplay(text), color=discord.Color.dark_grey()))
