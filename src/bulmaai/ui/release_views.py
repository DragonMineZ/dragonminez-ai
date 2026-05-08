from collections.abc import Awaitable, Callable
from dataclasses import replace

import discord

from bulmaai.services.release_approval import ReleaseCandidate
from bulmaai.utils.permissions import is_admin


ReleaseAction = Callable[[discord.Interaction, ReleaseCandidate], Awaitable[None]]


def can_manage_release_approval(user: object) -> bool:
    return is_admin(user)  # type: ignore[arg-type]


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def build_release_candidate_embed(
    candidate: ReleaseCandidate,
    *,
    status: str = "Pending approval",
    actor: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"DragonMineZ {candidate.version} release candidate",
        url=candidate.workflow_run_url,
        color=discord.Color.gold(),
    )
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Release Type", value=candidate.release_type, inline=True)
    embed.add_field(name="Minecraft", value=candidate.minecraft_version, inline=True)
    embed.add_field(name="Forge", value=candidate.forge_version, inline=True)
    embed.add_field(name="Commit", value=f"`{candidate.commit_sha}`", inline=False)
    embed.add_field(name="Artifact", value=f"`{candidate.artifact_name}`", inline=False)
    embed.add_field(name="Artifact SHA-256", value=f"`{candidate.artifact_sha256}`", inline=False)
    embed.add_field(name="Targets", value=", ".join(candidate.targets), inline=True)

    if candidate.changelog:
        embed.add_field(
            name="Changelog",
            value=_truncate(candidate.changelog, 1024),
            inline=False,
        )
    if candidate.update_description:
        embed.add_field(
            name="Update Description",
            value=_truncate(candidate.update_description, 1024),
            inline=False,
        )
    if actor:
        embed.set_footer(text=actor)

    return embed


class ReleaseMetadataModal(discord.ui.Modal):
    def __init__(self, candidate: ReleaseCandidate):
        super().__init__(title=f"Modify {candidate.version} publishing args")
        self.candidate = candidate
        self.result: ReleaseCandidate | None = None

        self.changelog_input = discord.ui.InputText(
            label="Changelog",
            placeholder="Markdown release notes for Modrinth and CurseForge",
            style=discord.InputTextStyle.long,
            required=False,
            max_length=4000,
            value=candidate.changelog or "",
        )
        self.update_description_input = discord.ui.InputText(
            label="Update Description",
            placeholder="Short text for Forge update.json",
            style=discord.InputTextStyle.long,
            required=False,
            max_length=1000,
            value=candidate.update_description or "",
        )
        self.add_item(self.changelog_input)
        self.add_item(self.update_description_input)

    async def callback(self, interaction: discord.Interaction):
        self.result = replace(
            self.candidate,
            changelog=self.changelog_input.value.strip() or None,
            update_description=self.update_description_input.value.strip() or None,
        )
        await interaction.response.defer()


class ReleaseCandidateView(discord.ui.View):
    def __init__(
        self,
        candidate: ReleaseCandidate,
        *,
        on_approve: ReleaseAction,
        on_reject: ReleaseAction,
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        self.candidate = candidate
        self._on_approve = on_approve
        self._on_reject = on_reject

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if can_manage_release_approval(interaction.user):
            return True
        await interaction.response.send_message(
            "Only Discord administrators can manage release approvals.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._require_admin(interaction):
            return
        await self._on_approve(interaction, self.candidate)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._require_admin(interaction):
            return
        await self._on_reject(interaction, self.candidate)

    @discord.ui.button(label="Modify", style=discord.ButtonStyle.primary)
    async def modify_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not await self._require_admin(interaction):
            return

        modal = ReleaseMetadataModal(self.candidate)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.result is None:
            return

        self.candidate = modal.result
        if interaction.message is not None:
            await interaction.message.edit(
                embed=build_release_candidate_embed(
                    self.candidate,
                    status="Pending approval",
                    actor=f"Modified by {interaction.user}",
                ),
                view=self,
            )
        await interaction.followup.send("Release publishing args updated.", ephemeral=True)
