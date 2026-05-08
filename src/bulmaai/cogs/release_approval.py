import asyncio
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
from bulmaai.services.release_webhook import ReleaseWebhookServer
from bulmaai.ui.release_views import (
    ReleaseCandidateView,
    build_release_candidate_embed,
)
from bulmaai.utils.permissions import is_admin


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


def _parse_targets(raw_targets: str) -> tuple[str, ...]:
    targets = tuple(part.strip() for part in raw_targets.split(",") if part.strip())
    return targets or ("modrinth", "curseforge")


class ReleaseApprovalCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.approval_service = ReleaseApprovalService(
            github_service=_get_release_github_service(self.settings),
        )
        self.webhook_server: ReleaseWebhookServer | None = None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._start_webhook_server()

    def cog_unload(self) -> None:
        if self.webhook_server is not None:
            self.webhook_server.stop()
            self.webhook_server = None

    def _start_webhook_server(self) -> None:
        if not self.settings.release_webhook_enabled:
            return
        if self.webhook_server is not None:
            return

        self.webhook_server = ReleaseWebhookServer(
            host=self.settings.release_webhook_host,
            port=self.settings.release_webhook_port,
            path=self.settings.release_webhook_path,
            secret=self.settings.release_webhook_secret,
            loop=asyncio.get_running_loop(),
            on_payload=self._handle_webhook_payload,
        )
        self.webhook_server.start()

    release = discord.SlashCommandGroup("release", "DragonMineZ release approval commands")

    @release.command(name="candidate", description="Post a DragonMineZ release candidate for approval")
    @discord.option("version", description="Release version", required=True)
    @discord.option("commit_sha", description="Approved main commit SHA", required=True)
    @discord.option("artifact_sha256", description="Candidate artifact SHA-256", required=True)
    @discord.option("artifact_name", description="Candidate jar artifact name", required=True)
    @discord.option("changelog", description="Markdown changelog for Modrinth and CurseForge", required=True)
    @discord.option("update_description", description="Forge update.json description", required=True)
    @discord.option("targets", description="Comma-separated publish targets", required=False)
    @discord.option("release_type", description="Release type", required=False)
    @discord.option("minecraft_version", description="Minecraft version", required=False)
    @discord.option("forge_version", description="Forge version", required=False)
    @discord.option("workflow_run_url", description="GitHub Actions run URL", required=False)
    async def post_manual_candidate(
        self,
        ctx: discord.ApplicationContext,
        version: str,
        commit_sha: str,
        artifact_sha256: str,
        artifact_name: str,
        changelog: str | None = None,
        update_description: str | None = None,
        targets: str = "modrinth,curseforge",
        release_type: str = "release",
        minecraft_version: str = "1.20.1",
        forge_version: str = "47.4.10",
        workflow_run_url: str | None = None,
    ) -> None:
        if not is_admin(ctx.author):
            return await ctx.respond("Only Discord administrators can post release candidates.")

        await ctx.defer(ephemeral=True)
        candidate = ReleaseCandidate(
            version=version.strip(),
            release_type=release_type.strip(),
            minecraft_version=minecraft_version.strip(),
            forge_version=forge_version.strip(),
            commit_sha=commit_sha.strip(),
            artifact_name=artifact_name.strip(),
            artifact_sha256=artifact_sha256.strip(),
            targets=_parse_targets(targets),
            workflow_run_url=workflow_run_url.strip() if workflow_run_url else None,
            changelog=changelog.strip() if changelog and changelog.strip() else None,
            update_description=(
                update_description.strip()
                if update_description and update_description.strip()
                else None
            ),
        )
        await self.post_candidate(candidate)
        await ctx.followup.send("Release candidate posted for approval.", ephemeral=True)

    async def _handle_webhook_payload(self, payload: dict) -> None:
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
    ) -> None:
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
            return
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

    async def _reject_candidate(
        self,
        interaction: discord.Interaction,
        candidate: ReleaseCandidate,
    ) -> None:
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


def setup(bot: discord.Bot):
    bot.add_cog(ReleaseApprovalCog(bot))
