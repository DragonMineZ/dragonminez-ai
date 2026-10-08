import re

import discord

MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

PATREON_WELCOME_VERIFY_CUSTOM_ID = "patreon_welcome:verify"


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

    def __init__(self, *, on_submit, title: str = "DragonMineZ Beta Access"):
        super().__init__(title=title)
        self.on_submit_callback = on_submit  # async (interaction, username) -> None
        self.username = discord.ui.InputText(
            label="Minecraft username",
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

    def __init__(self, *, requester_id: int, old_nickname: str, new_nickname: str, on_confirm):
        self.requester_id = requester_id
        self.old_nickname = old_nickname
        self.new_nickname = new_nickname
        self.on_confirm = on_confirm  # async (interaction) -> None
        self._submitted = False
        yes = discord.ui.Button(label="Yes", style=discord.ButtonStyle.success)
        no = discord.ui.Button(label="No", style=discord.ButtonStyle.danger)
        yes.callback, no.callback = self._yes, self._no
        super().__init__(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "### 🔁 Update your username?\n"
                    "You're already whitelisted, but we can update your username: "
                    f"`{old_nickname}` will be changed to `{new_nickname}`. Continue?"
                ),
                discord.ui.ActionRow(yes, no),
                color=discord.Colour.from_rgb(255, 85, 0),
            ),
            timeout=300,
        )

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the command author can use these buttons.", ephemeral=True)
            return False
        if self._submitted:
            await interaction.response.send_message("This username update was already submitted.", ephemeral=True)
            return False
        return True

    async def _yes(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        self._submitted = True
        await interaction.response.defer()
        await _edit_interaction_message(interaction, view=_status(f"⏳ Updating `{self.old_nickname}` to `{self.new_nickname}`…"))
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
        await _edit_interaction_message(interaction, view=_status("Username update cancelled."))


def _status(text: str) -> discord.ui.DesignerView:
    return discord.ui.DesignerView(discord.ui.Container(discord.ui.TextDisplay(text), color=discord.Color.dark_grey()))
