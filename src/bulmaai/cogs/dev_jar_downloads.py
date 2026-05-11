import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import discord
from discord.ext import commands

from bulmaai.services.dev_jar_downloads import (
    DevJarArtifact,
    DevJarUploadPayload,
    OneTimeDownloadTokenStore,
    PatreonOAuthClient,
    build_oauth_state,
    build_patreon_authorization_url,
    find_latest_dev_jar,
    has_active_patreon_membership,
    parse_dev_jar_upload_payload,
    parse_dev_jar_filename,
    parse_oauth_state,
)
from bulmaai.services.release_webhook import (
    ReleaseWebhookHttpResponse,
    register_extra_webhook_route,
    register_extra_get_route,
    text_http_response,
    unregister_extra_webhook_route,
    unregister_extra_get_route,
)
from bulmaai.utils.permissions import has_any_allowed_role, is_admin


log = logging.getLogger(__name__)

DOWNLOAD_BUTTON_PREFIX = "dev_jar_download:"
DEV_JAR_EMBED_COLOR = discord.Colour.from_rgb(46, 204, 113)


def can_bypass_patreon_oauth(member: object, *, bypass_role_ids: tuple[int, ...]) -> bool:
    return is_admin(member) or has_any_allowed_role(member, bypass_role_ids)  # type: ignore[arg-type]


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Unknown"
    size_mb = size_bytes / (1024 * 1024)
    return f"{size_mb:.1f} MB"


def build_dev_jar_download_embed(
    artifact: DevJarArtifact,
    *,
    sha256: str | None = None,
    workflow_run_url: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="DragonMineZ dev jar",
        description="Latest GitHub build is ready for authorized Patreon download.",
        url=workflow_run_url,
        colour=DEV_JAR_EMBED_COLOR,
        timestamp=artifact.modified_at,
    )
    embed.add_field(name="Version", value=f"`{artifact.version}`", inline=True)
    embed.add_field(name="Branch", value=f"`{artifact.branch_slug}`", inline=True)
    embed.add_field(name="Commit", value=f"`{artifact.commit_sha}`", inline=True)
    embed.add_field(name="Artifact", value=f"`{artifact.file_name}`", inline=False)
    embed.add_field(name="Size", value=_format_size(artifact.size_bytes), inline=True)
    if sha256:
        embed.add_field(name="SHA-256", value=f"`{sha256}`", inline=False)
    embed.set_footer(text="Staff/admin bypass Patreon OAuth; other downloads require Patreon authorization.")
    return embed


class DevJarDownloadView(discord.ui.View):
    def __init__(self, artifact: DevJarArtifact):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Get download link",
                style=discord.ButtonStyle.primary,
                custom_id=f"{DOWNLOAD_BUTTON_PREFIX}{artifact.file_name}",
            )
        )


class DevJarDownloadsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.token_store = OneTimeDownloadTokenStore(now=time.time)
        self._release_webhook_route_registered = False
        self._release_get_routes_registered = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._register_release_webhook_route()
        self._register_release_get_routes()

    def cog_unload(self) -> None:
        unregister_extra_webhook_route(self.settings.dev_jar_download_webhook_path)
        unregister_extra_get_route(self.settings.dev_jar_download_oauth_callback_path)
        unregister_extra_get_route(f"{self.settings.dev_jar_download_download_path.rstrip('/')}/")

    def _register_release_webhook_route(self) -> None:
        if self._release_webhook_route_registered:
            return
        self._release_webhook_route_registered = True
        if not self.settings.dev_jar_download_enabled:
            return
        if not self.settings.release_webhook_secret:
            log.error("DMZ_RELEASE_BOT_WEBHOOK_SECRET is missing; release webhook dev-jar route skipped.")
            return

        loop = asyncio.get_running_loop()

        def submit_payload(payload: DevJarUploadPayload) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._handle_upload_payload(payload),
                loop,
            )

            def log_result(done_future: asyncio.Future[None]) -> None:
                try:
                    done_future.result()
                except Exception:
                    log.exception("Dev jar upload payload handling failed")

            future.add_done_callback(log_result)

        register_extra_webhook_route(
            path=self.settings.dev_jar_download_webhook_path,
            secret=self.settings.release_webhook_secret,
            secret_header="X-DMZ-Release-Bot-Secret",
            parse_payload=parse_dev_jar_upload_payload,
            submit_payload=submit_payload,
            accepted_body="Dev jar upload queued",
        )

    def _register_release_get_routes(self) -> None:
        if self._release_get_routes_registered:
            return
        self._release_get_routes_registered = True
        if not self.settings.dev_jar_download_enabled:
            return
        if not self.settings.dev_jar_download_upload_dir:
            log.error("DEV_JAR_DOWNLOAD_UPLOAD_DIR is missing; dev jar download routes skipped.")
            return

        loop = asyncio.get_running_loop()
        direct_prefix = f"{self.settings.dev_jar_download_download_path.rstrip('/')}/"

        def handle_oauth_callback(path: str, query: dict[str, list[str]]) -> ReleaseWebhookHttpResponse:
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not code or not state:
                return text_http_response(400, "Missing OAuth code or state")
            future = asyncio.run_coroutine_threadsafe(
                self._handle_oauth_callback(code, state),
                loop,
            )
            try:
                return future.result(timeout=30)
            except Exception:
                log.exception("Dev jar OAuth callback handling failed")
                return text_http_response(500, "Download authorization failed")

        def handle_direct_download(path: str, query: dict[str, list[str]]) -> ReleaseWebhookHttpResponse:
            token = path.removeprefix(direct_prefix)
            return self._handle_direct_token(token)

        register_extra_get_route(
            path_prefix=self.settings.dev_jar_download_oauth_callback_path,
            handle_request=handle_oauth_callback,
        )
        register_extra_get_route(
            path_prefix=direct_prefix,
            handle_request=handle_direct_download,
        )

    def _upload_dir(self) -> Path:
        if not self.settings.dev_jar_download_upload_dir:
            raise RuntimeError("DEV_JAR_DOWNLOAD_UPLOAD_DIR is not configured")
        return Path(self.settings.dev_jar_download_upload_dir)

    def _public_base_url(self) -> str:
        value = (self.settings.dev_jar_download_public_base_url or "").strip().rstrip("/")
        if not value:
            raise RuntimeError("DEV_JAR_DOWNLOAD_PUBLIC_BASE_URL is not configured")
        return value

    def _oauth_redirect_uri(self) -> str:
        configured = (self.settings.patreon_oauth_redirect_uri or "").strip()
        if configured:
            return configured
        return f"{self._public_base_url()}{self.settings.dev_jar_download_oauth_callback_path}"

    def _direct_download_url(self, token: str) -> str:
        path = self.settings.dev_jar_download_download_path.rstrip("/")
        return f"{self._public_base_url()}{path}/{quote(token, safe='')}"

    def _artifact_path(self, artifact: DevJarArtifact) -> Path:
        path = artifact.resolve_path(self._upload_dir())
        if not path.is_file():
            raise FileNotFoundError(artifact.file_name)
        return path

    async def _resolve_channel(self) -> discord.abc.Messageable:
        channel_id = self.settings.dev_jar_download_channel_id
        if channel_id is None:
            raise RuntimeError("dev_jar_download_channel_id is not configured")
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError(f"Configured dev jar channel {channel_id} is not messageable")
        return channel

    async def _post_download_announcement(
        self,
        artifact: DevJarArtifact,
        *,
        channel: discord.abc.Messageable | None = None,
        sha256: str | None = None,
        workflow_run_url: str | None = None,
    ) -> None:
        target_channel = channel or await self._resolve_channel()
        await target_channel.send(
            embed=build_dev_jar_download_embed(
                artifact,
                sha256=sha256,
                workflow_run_url=workflow_run_url,
            ),
            view=DevJarDownloadView(artifact),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.slash_command(name="post-download", description="Post the latest DragonMineZ dev jar download announcement")
    @discord.option(
        "file_name",
        description="Specific uploaded jar filename; defaults to the latest dev jar",
        required=False,
    )
    @discord.option(
        "channel",
        description="Channel to post the announcement in (default: configured dev jar channel)",
        required=False,
    )
    async def post_download(
        self,
        ctx: discord.ApplicationContext,
        file_name: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        author = ctx.author
        if not can_bypass_patreon_oauth(
            author,
            bypass_role_ids=tuple(self.settings.dev_jar_download_bypass_role_ids),
        ):
            await ctx.respond("Only staff can post dev jar download announcements.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        try:
            if file_name:
                artifact = parse_dev_jar_filename(file_name.strip())
                path = self._artifact_path(artifact)
                stat = path.stat()
                artifact = DevJarArtifact(
                    file_name=artifact.file_name,
                    version=artifact.version,
                    branch_slug=artifact.branch_slug,
                    commit_sha=artifact.commit_sha,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            else:
                artifact = find_latest_dev_jar(self._upload_dir())
            target_channel = channel or await self._resolve_channel()
            await self._post_download_announcement(artifact, channel=target_channel)
        except Exception as error:
            log.exception("Failed to post dev jar download announcement")
            await ctx.followup.send(f"Failed to post download announcement: {error}", ephemeral=True)
            return

        await ctx.followup.send("Dev jar download announcement posted.", ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not isinstance(custom_id, str) or not custom_id.startswith(DOWNLOAD_BUTTON_PREFIX):
            return
        await self._handle_download_button(interaction, custom_id.removeprefix(DOWNLOAD_BUTTON_PREFIX))

    async def _handle_download_button(
        self,
        interaction: discord.Interaction,
        file_name: str,
    ) -> None:
        try:
            artifact = parse_dev_jar_filename(file_name)
            self._artifact_path(artifact)
            if can_bypass_patreon_oauth(
                interaction.user,
                bypass_role_ids=tuple(self.settings.dev_jar_download_bypass_role_ids),
            ):
                token = self.token_store.issue(
                    artifact=artifact,
                    requester_id=interaction.user.id,
                    ttl_seconds=self.settings.dev_jar_download_token_ttl_seconds,
                )
                await interaction.response.send_message(
                    f"One-time download link: {self._direct_download_url(token)}",
                    ephemeral=True,
                )
                return

            if not self.settings.patreon_oauth_client_id or not self.settings.patreon_oauth_client_secret:
                await interaction.response.send_message(
                    "Patreon download authorization is not configured yet.",
                    ephemeral=True,
                )
                return
            if not self.settings.release_webhook_secret:
                await interaction.response.send_message(
                    "Download signing is not configured yet.",
                    ephemeral=True,
                )
                return

            state = build_oauth_state(
                secret=self.settings.release_webhook_secret,
                artifact=artifact,
                requester_id=interaction.user.id,
                expires_at=int(time.time() + self.settings.dev_jar_download_token_ttl_seconds),
            )
            url = build_patreon_authorization_url(
                client_id=self.settings.patreon_oauth_client_id,
                redirect_uri=self._oauth_redirect_uri(),
                state=state,
                scope=self.settings.patreon_oauth_scope,
            )
            await interaction.response.send_message(
                f"Authorize with Patreon to download this build: {url}",
                ephemeral=True,
            )
        except Exception:
            log.exception("Failed to prepare dev jar download link")
            await interaction.response.send_message(
                "I could not prepare that download link. Ask staff to check the bot logs.",
                ephemeral=True,
            )

    def _handle_direct_token(self, token: str) -> ReleaseWebhookHttpResponse:
        claim = self.token_store.claim(token)
        if claim is None:
            return text_http_response(403, "Download link expired, already used, or already in use")
        try:
            path = self._artifact_path(claim.artifact)
        except (FileNotFoundError, ValueError):
            self.token_store.release_claim(claim)
            return text_http_response(404, "Artifact not found")
        return ReleaseWebhookHttpResponse(
            status=200,
            body=b"",
            content_type="application/java-archive",
            file_path=path,
            download_name=claim.artifact.file_name,
            on_stream_complete=lambda: self.token_store.complete_claim(claim),
            on_stream_error=lambda error: self.token_store.release_claim(claim),
        )

    async def _handle_oauth_callback(self, code: str, state: str) -> ReleaseWebhookHttpResponse:
        if not self.settings.release_webhook_secret:
            return text_http_response(500, "Download signing is not configured")
        parsed_state = parse_oauth_state(
            self.settings.release_webhook_secret,
            state,
            now=time.time,
        )
        if parsed_state is None:
            return text_http_response(403, "Download authorization expired")
        if not self.settings.patreon_oauth_client_id or not self.settings.patreon_oauth_client_secret:
            return text_http_response(500, "Patreon OAuth is not configured")

        client = PatreonOAuthClient(
            client_id=self.settings.patreon_oauth_client_id,
            client_secret=self.settings.patreon_oauth_client_secret,
            redirect_uri=self._oauth_redirect_uri(),
        )
        try:
            identity_payload = await client.fetch_identity_for_code(code)
        except Exception:
            log.exception("Patreon OAuth exchange failed for dev jar download")
            return text_http_response(403, "Patreon authorization failed")

        if not has_active_patreon_membership(
            identity_payload,
            campaign_id=self.settings.PATREON_CAMPAIGN_ID,
        ):
            return text_http_response(403, "Active DragonMineZ Patreon membership required")

        try:
            path = self._artifact_path(parsed_state.artifact)
        except (FileNotFoundError, ValueError):
            return text_http_response(404, "Artifact not found")

        return ReleaseWebhookHttpResponse(
            status=200,
            body=b"",
            content_type="application/java-archive",
            file_path=path,
            download_name=parsed_state.artifact.file_name,
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(DevJarDownloadsCog(bot))
