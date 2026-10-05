import logging

import discord
from discord.ext import commands

from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services.release_approval import (
    ReleaseApprovalService,
    ReleaseCandidate,
    ReleasePublishMetadataError,
    parse_release_candidate_payload,
)
from bulmaai.ui.release_views import (
    ReleaseCandidateView,
    build_release_candidate_embed,
)


log = logging.getLogger(__name__)


def _get_release_github_service(settings) -> GitHubService:
    auth = GitHubAppAuth(
        app_id=settings.GH_APP_ID,
        installation_id=settings.GH_INSTALLATION_ID,
        private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM,
    )
    return GitHubService(
        auth=auth,
        owner=settings.GITHUB_OWNER,
        repo=settings.GITHUB_DEFAULT_REPO,
        base_branch=settings.GITHUB_BASE_BRANCH,
    )


class ReleaseApprovalCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.approval_service = ReleaseApprovalService(
            github_service=_get_release_github_service(bot.settings),
        )

    @property
    def settings(self):
        return self.bot.settings

    async def handle_webhook_payload(self, payload: dict) -> None:
        candidate = parse_release_candidate_payload(payload)
        await self.post_candidate(candidate)

    async def post_candidate(self, candidate: ReleaseCandidate) -> None:
        channel_id = self.settings.releases_channel_id
        if channel_id is None:
            raise RuntimeError("releases_channel_id is not configured")

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)

        if not hasattr(channel, "send"):
            raise RuntimeError(f"Configured releases channel {channel_id} is not messageable")

        await channel.send(
            embed=build_release_candidate_embed(candidate),
            view=ReleaseCandidateView(
                candidate,
                on_approve=self._approve_candidate,
                on_reject=self._reject_candidate,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _approve_candidate(
        self,
        interaction: discord.Interaction,
        candidate: ReleaseCandidate,
    ) -> bool:
        await interaction.response.defer(ephemeral=True)
        try:
            await self.approval_service.approve_candidate(
                candidate,
                approved_by=str(interaction.user),
                changelog=candidate.changelog,
                update_description=candidate.update_description,
            )
        except ReleasePublishMetadataError as error:
            await interaction.followup.send(
                f"{error}. Use Modify to add release notes before approval.",
                ephemeral=True,
            )
            return False
        if interaction.message is not None:
            await interaction.message.edit(
                embed=build_release_candidate_embed(
                    candidate,
                    status="Approved",
                    actor=f"Approved by {interaction.user}",
                ),
                view=None,
            )
        await interaction.followup.send(
            f"DragonMineZ {candidate.version} approval dispatched to GitHub.",
            ephemeral=True,
        )
        return True

    async def _reject_candidate(
        self,
        interaction: discord.Interaction,
        candidate: ReleaseCandidate,
    ) -> bool:
        await interaction.response.defer(ephemeral=True)
        if interaction.message is not None:
            await interaction.message.edit(
                embed=build_release_candidate_embed(
                    candidate,
                    status="Rejected",
                    actor=f"Rejected by {interaction.user}",
                ),
                view=None,
            )
        await interaction.followup.send(
            f"DragonMineZ {candidate.version} release candidate rejected.",
            ephemeral=True,
        )
        return True


def setup(bot: discord.Bot):
    bot.add_cog(ReleaseApprovalCog(bot))
