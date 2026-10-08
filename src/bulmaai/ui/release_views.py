from collections.abc import Awaitable, Callable
from dataclasses import replace

import discord

from bulmaai.services.release_approval import ReleaseCandidate
from bulmaai.utils.permissions import is_admin


ReleaseAction = Callable[[discord.Interaction, ReleaseCandidate], Awaitable[bool]]  # True once decided


def can_manage_release_approval(user: object) -> bool:
    return is_admin(user)  # type: ignore[arg-type]


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


STATUS_COLORS = {"Approved": discord.Color.green(), "Rejected": discord.Color.red()}


def release_card_items(
    candidate: ReleaseCandidate,
    *,
    status: str = "Pending approval",
    actor: str | None = None,
) -> list[discord.ui.Item]:
    """The release candidate card's text: what's being shipped, then the notes that will go out."""
    head = f"## DragonMineZ {candidate.version} release candidate"
    if candidate.workflow_run_url:
        head += f"\n[Workflow run](<{candidate.workflow_run_url}>)"
    facts = (
        f"**Status** {status}　**Release type** {candidate.release_type}　"
        f"**Minecraft** {candidate.minecraft_version}　**Forge** {candidate.forge_version}\n"
        f"**Commit** `{candidate.commit_sha}`\n**Artifact** `{candidate.artifact_name}`\n"
        f"**Targets** {', '.join(candidate.targets)}"
    )
    items: list[discord.ui.Item] = [discord.ui.TextDisplay(head), discord.ui.TextDisplay(facts)]
    if candidate.changelog:
        items.append(discord.ui.TextDisplay(f"**Changelog**\n{_truncate(candidate.changelog, 1800)}"))
    if candidate.update_description:
        items.append(discord.ui.TextDisplay(f"**Update description**\n{_truncate(candidate.update_description, 800)}"))
    if actor:
        items.append(discord.ui.TextDisplay(f"-# {actor}"))
    return items


def release_card(candidate: ReleaseCandidate, *, status: str, actor: str | None = None) -> discord.ui.DesignerView:
    """A decided candidate: the card without buttons."""
    container = discord.ui.Container(
        *release_card_items(candidate, status=status, actor=actor), color=STATUS_COLORS.get(status, discord.Color.gold())
    )
    return discord.ui.DesignerView(container, timeout=None)


class ReleaseMetadataModal(discord.ui.Modal):
    def __init__(self, candidate: ReleaseCandidate):
        super().__init__(title=f"Modify {candidate.version} publishing args")
        self.candidate = candidate
        self.result: ReleaseCandidate | None = None

        self.changelog_input = discord.ui.InputText(
            label="Changelog",
            placeholder="Markdown release notes for Modrinth and CurseForge",
            style=discord.InputTextStyle.long,
            required=True,
            max_length=4000,
            value=candidate.changelog or "",
        )
        self.update_description_input = discord.ui.InputText(
            label="Update Description",
            placeholder="Short text for Forge update.json",
            style=discord.InputTextStyle.long,
            required=True,
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


class ReleaseCandidateView(discord.ui.DesignerView):
    """The pending card itself, with Approve / Reject / Modify. In-memory: a restart drops the buttons' callbacks."""

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
        self._handled = False
        self.approve_button = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success)
        self.reject_button = discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger)
        self.modify_button = discord.ui.Button(label="Modify", style=discord.ButtonStyle.primary)
        self.approve_button.callback = lambda interaction: self._decide(interaction, self._on_approve)
        self.reject_button.callback = lambda interaction: self._decide(interaction, self._on_reject)
        self.modify_button.callback = self._modify
        self._render()

    def _render(self, actor: str | None = None) -> None:
        self.clear_items()
        self.add_item(
            discord.ui.Container(
                *release_card_items(self.candidate, actor=actor),
                discord.ui.ActionRow(self.approve_button, self.reject_button, self.modify_button),
                color=discord.Color.gold(),
            )
        )

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if can_manage_release_approval(interaction.user):
            return True
        await interaction.response.send_message(
            "Only Discord administrators can manage release approvals.",
            ephemeral=True,
        )
        return False

    async def _decide(self, interaction: discord.Interaction, action: ReleaseAction) -> None:
        if not await self._require_admin(interaction):
            return
        if self._handled:
            await interaction.response.send_message("This release is already being handled.", ephemeral=True)
            return
        # Claimed before the first await, so a double or concurrent click can't dispatch two approvals;
        # released only when the action didn't decide (e.g. missing release notes).
        self._handled = True
        decided = False
        try:
            decided = await action(interaction, self.candidate)
        finally:
            self._handled = decided

    async def _modify(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return

        modal = ReleaseMetadataModal(self.candidate)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.result is None or self._handled:  # don't bring the buttons back on a decided release
            return

        self.candidate = modal.result
        if interaction.message is not None:
            self._render(actor=f"Modified by {interaction.user}")
            await interaction.message.edit(view=self)
        await interaction.followup.send(f"✏️ Release publishing args updated by {interaction.user.mention}.", allowed_mentions=discord.AllowedMentions.none())
