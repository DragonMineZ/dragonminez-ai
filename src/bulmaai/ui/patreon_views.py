import re

import discord

from bulmaai.utils.permissions import is_admin

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


class NicknameModal(discord.ui.Modal):
    def __init__(self, title: str = "Set Minecraft nickname"):
        super().__init__(title=title)
        self.nick = discord.ui.InputText(
            label="Minecraft nickname",
            placeholder="e.g. Bruno_123",
            min_length=3,
            max_length=16,
            required=True,
        )
        self.add_item(self.nick)
        self.value: str | None = None

    async def callback(self, interaction: discord.Interaction):
        self.value = self.nick.value.strip()
        await interaction.response.defer(ephemeral=True)


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


class PatreonWelcomeView(discord.ui.View):
    """Quick-start buttons attached to the Patreon welcome DM."""

    def __init__(self, *, downloads_channel_url: str | None = None):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Verify & Get Beta Access",
                style=discord.ButtonStyle.success,
                custom_id=PATREON_WELCOME_VERIFY_CUSTOM_ID,
            )
        )
        if downloads_channel_url:
            self.add_item(
                discord.ui.Button(
                    label="Open Downloads Channel",
                    url=downloads_channel_url,
                )
            )


class UserConfirmView(discord.ui.View):
    def __init__(self, *, requester_id: int, nickname: str, on_confirm):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.nickname = nickname
        self.on_confirm = on_confirm  # async (interaction, nickname) -> None
        self._submitted = False

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the command author can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        if self._submitted:
            await interaction.response.send_message("This request was already submitted.", ephemeral=True)
            return
        self._submitted = True
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await _edit_interaction_message(
            interaction,
            content=f"Checking `{self.nickname}`...",
            view=None,
        )
        try:
            await self.on_confirm(interaction, self.nickname)
        except Exception:
            self._submitted = False
            for child in self.children:
                child.disabled = False
            raise

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return

        modal = NicknameModal(title="Edit Minecraft nickname")
        await interaction.response.send_modal(modal)
        await modal.wait()

        new_nick = (modal.value or "").strip()
        if not MC_NAME_RE.match(new_nick):
            return await _edit_interaction_message(
                interaction,
                content="Invalid nickname (3-16 chars, letters/numbers/_). Try again.",
                view=self,
            )

        self.nickname = new_nick
        await _edit_interaction_message(
            interaction,
            content=f"Confirm `{self.nickname}` as your Minecraft username?",
            view=self,
        )


class UsernameUpdateConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        requester_id: int,
        old_nickname: str,
        new_nickname: str,
        on_confirm,
    ):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.old_nickname = old_nickname
        self.new_nickname = new_nickname
        self.on_confirm = on_confirm  # async (interaction) -> None
        self._submitted = False

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the command author can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        if self._submitted:
            await interaction.response.send_message("This username update was already submitted.", ephemeral=True)
            return
        self._submitted = True
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await _edit_interaction_message(
            interaction,
            content=f"Updating `{self.old_nickname}` to `{self.new_nickname}`...",
            view=None,
        )
        try:
            await self.on_confirm(interaction)
        except Exception:
            self._submitted = False
            for child in self.children:
                child.disabled = False
            raise

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        if self._submitted:
            await interaction.response.send_message("This username update was already submitted.", ephemeral=True)
            return
        self._submitted = True
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await _edit_interaction_message(
            interaction,
            content="Username update cancelled.",
            view=None,
        )


class AdminPRView(discord.ui.View):
    def __init__(self, *, pr_number: int, nickname: str, branch: str, on_confirm, on_edit, on_reject):
        super().__init__(timeout=86400)
        self.pr_number = pr_number
        self.nickname = nickname
        self.branch = branch
        self.on_confirm = on_confirm  # async (interaction) -> None
        self.on_edit = on_edit        # async (interaction, new_nick) -> None
        self.on_reject = on_reject    # async (interaction) -> None
        self._terminal_action_running = False
        self._terminal_action_finished = False

    async def _admin_only(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member or not is_admin(member):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True

    async def _send_already_busy(self, interaction: discord.Interaction) -> None:
        if self._terminal_action_finished:
            await interaction.response.send_message("This request was already handled.", ephemeral=True)
            return
        await interaction.response.send_message("This request is already being processed.", ephemeral=True)

    async def _begin_terminal_action(self, interaction: discord.Interaction) -> bool:
        if self._terminal_action_running or self._terminal_action_finished:
            await self._send_already_busy(interaction)
            return False

        self._terminal_action_running = True
        for child in self.children:
            child.disabled = True

        await interaction.response.defer()
        message = getattr(interaction, "message", None)
        if message is not None:
            try:
                await message.edit(view=self)
            except discord.HTTPException:
                pass
        return True

    async def _restore_terminal_action(self, interaction: discord.Interaction) -> None:
        self._terminal_action_running = False
        for child in self.children:
            child.disabled = False
        message = getattr(interaction, "message", None)
        if message is not None:
            try:
                await message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._admin_only(interaction):
            return
        if not await self._begin_terminal_action(interaction):
            return
        try:
            await self.on_confirm(interaction)
        except Exception:
            await self._restore_terminal_action(interaction)
            raise
        self._terminal_action_finished = True

    @discord.ui.button(label="Edit Nickname", style=discord.ButtonStyle.secondary)
    async def edit_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._admin_only(interaction):
            return
        if self._terminal_action_running or self._terminal_action_finished:
            await self._send_already_busy(interaction)
            return

        modal = NicknameModal(title="Edit nickname (admin)")
        await interaction.response.send_modal(modal)
        await modal.wait()

        new_nick = (modal.value or "").strip()
        if not MC_NAME_RE.match(new_nick):
            return await interaction.followup.send("Invalid nickname format.", ephemeral=True)

        await self.on_edit(interaction, new_nick)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._admin_only(interaction):
            return
        if not await self._begin_terminal_action(interaction):
            return
        try:
            await self.on_reject(interaction)
        except Exception:
            await self._restore_terminal_action(interaction)
            raise
        self._terminal_action_finished = True
