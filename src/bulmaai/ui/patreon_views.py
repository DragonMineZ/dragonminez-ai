import re
import discord

from bulmaai.utils.permissions import is_admin

MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


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

class UserConfirmView(discord.ui.View):
    def __init__(self, *, requester_id: int, nickname: str, on_confirm):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.nickname = nickname
        self.on_confirm = on_confirm  # async (interaction, nickname) -> None

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the command author can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.on_confirm(interaction, self.nickname)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return

        modal = NicknameModal(title="Edit Minecraft nickname")
        await interaction.response.send_modal(modal)
        await modal.wait()

        new_nick = (modal.value or "").strip()
        if not MC_NAME_RE.match(new_nick):
            return await interaction.followup.send(
                "Invalid nickname (3–16 chars, letters/numbers/_). Try again.",
                ephemeral=True,
            )

        self.nickname = new_nick
        await interaction.followup.send(
            f"You've said that `{self.nickname}` is your Minecraft nickname to get access to the Patreon-only releases, is this correct?",
            ephemeral=True,
            view=self,
        )

class AdminPRView(discord.ui.View):
    def __init__(self, *, pr_number: int, nickname: str, on_confirm, on_edit, on_reject):
        super().__init__(timeout=86400)
        self.pr_number = pr_number
        self.nickname = nickname
        self.on_confirm = on_confirm  # async (interaction) -> None
        self.on_edit = on_edit        # async (interaction, new_nick) -> None
        self.on_reject = on_reject    # async (interaction) -> None

    async def _admin_only(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member or not is_admin(member):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._admin_only(interaction):
            return
        await interaction.response.defer()
        await self.on_confirm(interaction)
        for c in self.children:
            c.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Edit Nickname", style=discord.ButtonStyle.secondary)
    async def edit_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._admin_only(interaction):
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
        await interaction.response.defer()
        await self.on_reject(interaction)
        for c in self.children:
            c.disabled = True
        await interaction.message.edit(view=self)
