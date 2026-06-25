import asyncio
import html
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import discord
from discord.ext import commands

from bulmaai.services.dev_jar_download_records import (
    has_completed_dev_jar_download,
    record_completed_dev_jar_download,
)
from bulmaai.services.dev_jar_downloads import (
    DevJarArtifact,
    DevJarCommit,
    DevJarDownloadClaim,
    DevJarUploadPayload,
    OneTimeDownloadTokenStore,
    find_latest_dev_jar,
    iter_dev_jars,
    parse_dev_jar_upload_payload,
    parse_dev_jar_filename,
)
from bulmaai.services.patch_notes import PATCH_NOTES_URL
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
MANUAL_DOWNLOAD_BUTTON_PREFIX = "dev_jar_download:manual:"
DOWNLOAD_FILE_SUFFIX = "/file"
DEV_JAR_EMBED_COLOR = discord.Colour.from_rgb(46, 204, 113)
DEV_JAR_ANNOUNCEMENT_CHANNEL_IDS = (
    1516564287210913932,
    1453303311330709674,
)
DEV_JAR_PATREON_ROLE_IDS = (
    1287877272224665640,
    1287877305259130900,
)
DEV_JAR_TESTER_ROLE_IDS = (1286814599215317034,)


def can_post_download_announcement(member: object, *, staff_role_ids: tuple[int, ...]) -> bool:
    return is_admin(member) or has_any_allowed_role(member, staff_role_ids)  # type: ignore[arg-type]


def can_download_dev_jar(member: object) -> bool:
    return (
        is_admin(member)  # type: ignore[arg-type]
        or has_any_allowed_role(member, DEV_JAR_PATREON_ROLE_IDS)  # type: ignore[arg-type]
        or has_any_allowed_role(member, DEV_JAR_TESTER_ROLE_IDS)  # type: ignore[arg-type]
    )


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Unknown"
    size_mb = size_bytes / (1024 * 1024)
    return f"{size_mb:.3f} MB"


def _truncate_commit_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _manual_artifact_commit(artifact: DevJarArtifact, *, author: object) -> DevJarCommit:
    return DevJarCommit(
        sha=artifact.commit_sha,
        title="Manual dev jar announcement",
        description=None,
        author=str(author),
        url=f"https://github.com/DragonMineZ/dragonminez/commit/{artifact.commit_sha}",
    )


def _format_commit_summary(commits: tuple[DevJarCommit, ...]) -> str:
    lines: list[str] = []
    for index, commit in enumerate(commits):
        short_sha = commit.sha[:7]
        title = _truncate_commit_text(commit.title, 140)
        author = _truncate_commit_text(commit.author, 60)
        entry = f"[{short_sha}]({commit.url}) {title} - {author}"
        if commit.description:
            entry = f"{entry}\n{_truncate_commit_text(commit.description, 220)}"
        candidate_lines = [*lines, entry]
        candidate = "\n".join(candidate_lines)
        if len(candidate) <= 1024:
            lines = candidate_lines
            continue
        remaining = len(commits) - index
        suffix = f"...and {remaining} more commit{'s' if remaining != 1 else ''}."
        candidate = "\n".join([*lines, suffix])
        if len(candidate) <= 1024:
            lines.append(suffix)
        break
    return "\n".join(lines)


def build_dev_jar_download_embed(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    previous_size_bytes: int | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="DragonMineZ Dev Update",
        description="A push has been detected in GitHub and a .jar has successfully passed tests and is ready to be "
                    "downloaded! These versions are automatically built by the latest commits, meaning they can be "
                    "unstable or not run at all on your machine. For stable (and mostly tested) beta/alpha releases, "
                    "look for them in Discord. Click the button to download the latest dev jar, and check the "
                    "changelog for details on what changed.",
        url=workflow_run_url,
        colour=DEV_JAR_EMBED_COLOR,
        timestamp=artifact.modified_at,
    )
    embed.add_field(name="Version", value=f"`{artifact.version}`", inline=True)
    embed.add_field(name="Commit .jar", value=f"`{artifact.commit_sha}`", inline=True)
    embed.add_field(name="Artifact", value=f"`{artifact.file_name}`", inline=False)

    size_str = _format_size(artifact.size_bytes)
    if previous_size_bytes is not None:
        size_str = f"{_format_size(previous_size_bytes)} → {size_str}"
    embed.add_field(name="Size", value=size_str, inline=True)
    if sha256:
        embed.add_field(name="SHA-256", value=f"`{sha256}`", inline=False)
    embed.add_field(name="Commits Changelog", value=_format_commit_summary(commits), inline=False)
    notes_day = _artifact_day(artifact)
    embed.add_field(
        name="Patch Notes",
        value=(
            f"The **Patch Notes** button below opens the v2.1 patch notes for {notes_day} "
            "with everything that changed in this update."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Downloads require Discord access authorization. Download links are one-time per user per jar."
    )
    return embed


def _artifact_day(artifact: DevJarArtifact) -> str:
    moment = artifact.modified_at or datetime.now(timezone.utc)
    return f"{moment:%B %d, %Y}"


class DevJarDownloadView(discord.ui.View):
    def __init__(self, artifact: DevJarArtifact, *, is_manual: bool = False):
        super().__init__(timeout=None)
        prefix = MANUAL_DOWNLOAD_BUTTON_PREFIX if is_manual else DOWNLOAD_BUTTON_PREFIX
        self.add_item(
            discord.ui.Button(
                label="Get download link",
                style=discord.ButtonStyle.primary,
                custom_id=f"{prefix}{artifact.file_name}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label=f"Patch Notes – {_artifact_day(artifact)}",
                url=PATCH_NOTES_URL,
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

        direct_prefix = f"{self.settings.dev_jar_download_download_path.rstrip('/')}/"

        def handle_direct_download(path: str, query: dict[str, list[str]]) -> ReleaseWebhookHttpResponse:
            token_path = path.removeprefix(direct_prefix)
            if token_path.endswith(DOWNLOAD_FILE_SUFFIX):
                token = token_path[: -len(DOWNLOAD_FILE_SUFFIX)]
                return self._handle_direct_token_file(token)
            return self._handle_direct_token(token_path)

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

    def _direct_download_url(self, token: str) -> str:
        path = self.settings.dev_jar_download_download_path.rstrip("/")
        return f"{self._public_base_url()}{path}/{quote(token, safe='')}"

    def _direct_download_file_url(self, token: str) -> str:
        return f"{self._direct_download_url(token)}{DOWNLOAD_FILE_SUFFIX}"

    def _get_previous_artifact_size(self, current_file_name: str) -> int | None:
        try:
            artifacts = [
                a for a in iter_dev_jars(self._upload_dir())
                if a.file_name != current_file_name
            ]
            if not artifacts:
                return None
            latest = max(
                artifacts,
                key=lambda a: (
                    a.modified_at or datetime.min.replace(tzinfo=timezone.utc),
                    a.file_name,
                ),
            )
            return latest.size_bytes
        except Exception as e:
            log.exception(f"Failed to get previous artifact size: {e}")
            return None

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

    async def _resolve_announcement_channels(self) -> list[discord.abc.Messageable]:
        channels: list[discord.abc.Messageable] = []
        for channel_id in DEV_JAR_ANNOUNCEMENT_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            if not hasattr(channel, "send"):
                raise RuntimeError(f"Configured dev jar channel {channel_id} is not messageable")
            channels.append(channel)
        return channels

    async def _post_download_announcement(
        self,
        artifact: DevJarArtifact,
        *,
        commits: tuple[DevJarCommit, ...],
        channel: discord.abc.Messageable | None = None,
        sha256: str | None = None,
        workflow_run_url: str | None = None,
        previous_size_bytes: int | None = None,
        is_manual: bool = False,
    ) -> None:
        target_channels = [channel] if channel is not None else await self._resolve_announcement_channels()
        for target_channel in target_channels:
            await target_channel.send(
                embed=build_dev_jar_download_embed(
                    artifact,
                    commits=commits,
                    sha256=sha256,
                    workflow_run_url=workflow_run_url,
                    previous_size_bytes=previous_size_bytes,
                ),
                view=DevJarDownloadView(artifact, is_manual=is_manual),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _handle_upload_payload(self, payload: DevJarUploadPayload) -> None:
        path = self._artifact_path(payload.artifact)
        stat = path.stat()
        artifact = DevJarArtifact(
            file_name=payload.artifact.file_name,
            version=payload.artifact.version,
            commit_sha=payload.artifact.commit_sha,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )
        await self._post_download_announcement(
            artifact,
            commits=payload.commits,
            sha256=payload.sha256,
            workflow_run_url=payload.workflow_run_url,
            previous_size_bytes=self._get_previous_artifact_size(artifact.file_name),
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
        if not can_post_download_announcement(
            author,
            staff_role_ids=tuple(self.settings.discord_staff_role_ids),
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
                    commit_sha=artifact.commit_sha,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            else:
                artifact = find_latest_dev_jar(self._upload_dir())
            target_channel = channel or await self._resolve_channel()
            await self._post_download_announcement(
                artifact,
                commits=(_manual_artifact_commit(artifact, author=ctx.author),),
                channel=target_channel,
                previous_size_bytes=self._get_previous_artifact_size(artifact.file_name),
                is_manual=True,
            )
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
        if not isinstance(custom_id, str):
            return
        if custom_id.startswith(MANUAL_DOWNLOAD_BUTTON_PREFIX):
            await self._handle_download_button(
                interaction,
                custom_id.removeprefix(MANUAL_DOWNLOAD_BUTTON_PREFIX),
                is_manual=True,
            )
        elif custom_id.startswith(DOWNLOAD_BUTTON_PREFIX):
            await self._handle_download_button(interaction, custom_id.removeprefix(DOWNLOAD_BUTTON_PREFIX))

    async def _handle_download_button(
        self,
        interaction: discord.Interaction,
        file_name: str,
        *,
        is_manual: bool = False,
    ) -> None:
        try:
            artifact = parse_dev_jar_filename(file_name)
            self._artifact_path(artifact)
            guild_id = getattr(interaction, "guild_id", None)
            if guild_id is None:
                await interaction.response.send_message(
                    "Use this download button inside the DragonMineZ Discord server.",
                    ephemeral=True,
                )
                return
            if not can_download_dev_jar(interaction.user):
                await interaction.response.send_message(
                    "Your Discord account is not authorized for this download.",
                    ephemeral=True,
                )
                return
            if not self.settings.release_webhook_secret:
                await interaction.response.send_message(
                    "Download signing is not configured yet.",
                    ephemeral=True,
                )
                return
            if not is_manual and await self._user_already_downloaded(interaction.user.id, artifact.file_name):
                await interaction.response.send_message(
                    f"You already downloaded `{artifact.file_name}`. Download links are one-time "
                    "per user per jar, so this build cannot be requested again. A newer dev jar "
                    "announcement will come with a fresh download.",
                    ephemeral=True,
                )
                return

            token = self.token_store.issue(
                artifact=artifact,
                requester_id=interaction.user.id,
                ttl_seconds=self.settings.dev_jar_download_token_ttl_seconds,
                is_manual=is_manual,
            )
            url = self._direct_download_url(token)
            await interaction.response.send_message(
                f"One-time download link: {url}",
                ephemeral=True,
            )
        except FileNotFoundError:
            log.exception("Dev jar artifact not found for download button")
            await interaction.response.send_message(
                "Hmm.. It seems that the file to download is missing. Maybe this announcement is outdated? Check for newer bot messages with the button.",
                ephemeral=True,
            )
        except Exception:
            log.exception("Failed to prepare dev jar download link")
            await interaction.response.send_message(
                "I could not prepare that download link. Ask staff to check the bot logs.",
                ephemeral=True,
            )

    async def _user_already_downloaded(self, user_id: int, file_name: str) -> bool:
        try:
            return await has_completed_dev_jar_download(user_id, file_name)
        except Exception:
            # The database being unreachable should not break downloads outright;
            # the one-time token still protects each issued link.
            log.exception("Failed to check completed dev jar downloads; allowing link request")
            return False

    def _complete_download_claim(self, claim: DevJarDownloadClaim) -> None:
        self.token_store.complete_claim(claim)
        if claim.is_manual:
            return
        loop = getattr(getattr(self, "bot", None), "loop", None)
        if loop is None or not loop.is_running():
            return

        future = asyncio.run_coroutine_threadsafe(
            record_completed_dev_jar_download(claim.requester_id, claim.artifact.file_name),
            loop,
        )

        def log_result(done_future) -> None:
            try:
                done_future.result()
            except Exception:
                log.exception("Failed to record completed dev jar download")

        future.add_done_callback(log_result)

    def _handle_direct_token(self, token: str) -> ReleaseWebhookHttpResponse:
        artifact = self.token_store.peek(token)
        if artifact is None:
            return text_http_response(403, "Download link expired, already used, or already in use")
        try:
            self._artifact_path(artifact)
        except (FileNotFoundError, ValueError):
            return text_http_response(404, "Artifact not found")
        return self._download_success_response(
            artifact=artifact,
            download_url=self._direct_download_file_url(token),
        )

    def _download_success_response(
        self,
        *,
        artifact: DevJarArtifact,
        download_url: str,
    ) -> ReleaseWebhookHttpResponse:
        safe_name = html.escape(artifact.file_name)
        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>200 success</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #1f2937;
      background: #f8fafc;
    }}
    main {{
      width: min(92vw, 34rem);
      padding: 2rem;
      border: 1px solid #dbe3ef;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 12px 40px rgba(15, 23, 42, 0.08);
    }}
    h1 {{
      margin: 0 0 0.75rem;
      font-size: 1.5rem;
      line-height: 1.2;
    }}
    p {{
      margin: 0.75rem 0 0;
      line-height: 1.5;
    }}
    a {{
      color: #2563eb;
      overflow-wrap: anywhere;
    }}
  </style>
</head>
<body>
  <main>
    <h1>HTTP 200 - Success!</h1>
    <p>Dev Note: These versions are automatically built by the latest commits, meaning they can be unstable or not run at all on your machine.</p>
    <p>For stable (and mostly tested) beta/alpha releases, look for them in Discord.
    <p>The file should be downloading shortly. When the download finishes, you can close this window.</p>
    <p><strong>File:</strong> {safe_name}</p>
    <p>If the download does not start, <a id="download-link" href="{html.escape(download_url, quote=True)}">click here</a>.</p>
    <p>Thank you for your Support!</p>
    <p>- The DragonMineZ Team</p>
  </main>
  <script>
    const downloadUrl = {json.dumps(download_url)};
    window.addEventListener("load", () => {{
      const frame = document.createElement("iframe");
      frame.hidden = true;
      frame.src = downloadUrl;
      document.body.appendChild(frame);
    }});
  </script>
</body>
</html>
"""
        return ReleaseWebhookHttpResponse(
            status=200,
            body=body.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )

    def _handle_direct_token_file(self, token: str) -> ReleaseWebhookHttpResponse:
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
            on_stream_complete=lambda: self._complete_download_claim(claim),
            on_stream_error=lambda error: self.token_store.release_claim(claim),
        )

def setup(bot: discord.Bot) -> None:
    bot.add_cog(DevJarDownloadsCog(bot))
