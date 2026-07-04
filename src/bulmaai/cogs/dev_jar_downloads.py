import asyncio
import html
import io
import json
import logging
import time
from collections.abc import Iterable
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
    build_dev_jar_commit_layout,
    find_latest_dev_jar,
    iter_dev_jars,
    merge_dev_jar_commits,
    parse_dev_jar_upload_payload,
    parse_dev_jar_filename,
)
from bulmaai.services.dev_jar_pending_review import (
    PendingDevJarReview,
    clear_pending_dev_jar_review,
    clear_pending_dev_jar_review_message,
    get_pending_dev_jar_review,
    set_pending_dev_jar_review_message,
    upsert_pending_dev_jar_review,
)
from bulmaai.services.dev_jar_published_state import (
    get_published_dev_jar_file_name,
    set_published_dev_jar_file_name,
)
from bulmaai.services.patch_notes import build_patch_notes_url
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
# Stable custom_ids for the staff review prompt so its buttons survive restarts.
DEV_JAR_REVIEW_PUBLISH_ID = "dev_jar_review:publish"
DEV_JAR_REVIEW_DISCARD_ID = "dev_jar_review:discard"
DEV_JAR_EMBED_COLOR = discord.Colour.from_rgb(46, 204, 113)
DEV_JAR_REVIEW_EMBED_COLOR = discord.Colour.blurple()
# Discord caps embeds at 25 fields; leave headroom for trailing fields appended
# after the commit changelog (e.g. Patch Notes).
MAX_FIELDS_PER_EMBED = 24
OVERFLOW_COMMITS_FILENAME = "dev-jar-commits.md"
# Local-only endpoint (bound to 127.0.0.1) the VPS-side cleanup script polls to learn
# which dev jar files must survive pruning: the currently published build (its public
# download button is live) and the currently pending-review build (staff haven't
# decided on it yet).
PROTECTED_ARTIFACTS_PATH = "/dmz-dev-jar/protected"
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


def _manual_artifact_commit(artifact: DevJarArtifact, *, author: object) -> DevJarCommit:
    return DevJarCommit(
        sha=artifact.commit_sha,
        title="Manual dev jar announcement",
        description=None,
        author=str(author),
        url=f"https://github.com/DragonMineZ/dragonminez/commit/{artifact.commit_sha}",
    )


def _artifact_day(artifact: DevJarArtifact) -> str:
    moment = artifact.modified_at or datetime.now(timezone.utc)
    return f"{moment:%B %d, %Y}"


def _build_base_dev_jar_fields(
    artifact: DevJarArtifact,
    *,
    sha256: str | None,
    previous_size_bytes: int | None,
) -> list[tuple[str, str, bool]]:
    fields: list[tuple[str, str, bool]] = [
        ("Version", f"`{artifact.version}`", True),
        ("Commit .jar", f"`{artifact.commit_sha}`", True),
        ("Artifact", f"`{artifact.file_name}`", False),
    ]
    size_str = _format_size(artifact.size_bytes)
    if previous_size_bytes is not None:
        size_str = f"{_format_size(previous_size_bytes)} → {size_str}"
    fields.append(("Size", size_str, True))
    if sha256:
        fields.append(("SHA-256", f"`{sha256}`", False))
    return fields


def _append_fields_across_embeds(
    embeds: list[discord.Embed],
    fields: Iterable[tuple[str, str]],
    *,
    colour: discord.Colour,
    inline: bool = False,
) -> None:
    """Append (name, value) fields to the last embed, spilling into new embeds
    once the 25-fields-per-embed limit is hit."""
    current = embeds[-1]
    current_field_count = len(current.fields)
    for name, value in fields:
        if current_field_count >= MAX_FIELDS_PER_EMBED:
            current = discord.Embed(colour=colour)
            embeds.append(current)
            current_field_count = 0
        current.add_field(name=name, value=value, inline=inline)
        current_field_count += 1


def build_dev_jar_download_embeds(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    previous_size_bytes: int | None = None,
) -> tuple[list[discord.Embed], str | None]:
    """Build the public dev jar announcement embed(s).

    Returns the embeds to send plus the full, untruncated commit changelog
    text when the commit list is too large to fit inside Discord's embed
    character budget (to be attached as a file so nothing gets dropped).
    """
    description = (
        "A push has been detected in GitHub and a .jar has successfully passed tests and is ready to be "
        "downloaded! These versions are automatically built by the latest commits, meaning they can be "
        "unstable or not run at all on your machine. For stable (and mostly tested) beta/alpha releases, "
        "look for them in Discord. Click the button to download the latest dev jar, and check the "
        "changelog for details on what changed."
    )
    footer_text = "Downloads require Discord access authorization. Download links are one-time per user per jar."
    notes_day = _artifact_day(artifact)
    patch_notes_value = (
        f"The **Patch Notes** button below opens the patch notes for {notes_day} "
        "with everything that changed in this update."
    )
    base_fields = _build_base_dev_jar_fields(
        artifact, sha256=sha256, previous_size_bytes=previous_size_bytes
    )
    base_char_count = (
        len("DragonMineZ Dev Update")
        + len(description)
        + len(footer_text)
        + len("Patch Notes")
        + len(patch_notes_value)
        + sum(len(name) + len(value) for name, value, _ in base_fields)
    )
    layout = build_dev_jar_commit_layout(commits, base_char_count=base_char_count)

    primary = discord.Embed(
        title="DragonMineZ Dev Update",
        description=description,
        url=workflow_run_url,
        colour=DEV_JAR_EMBED_COLOR,
        timestamp=artifact.modified_at,
    )
    for name, value, inline in base_fields:
        primary.add_field(name=name, value=value, inline=inline)

    embeds = [primary]
    _append_fields_across_embeds(embeds, layout.fields, colour=DEV_JAR_EMBED_COLOR)
    _append_fields_across_embeds(
        embeds, [("Patch Notes", patch_notes_value)], colour=DEV_JAR_EMBED_COLOR
    )
    embeds[-1].set_footer(text=footer_text)

    overflow_text = layout.full_changelog_text if layout.overflowed else None
    return embeds, overflow_text


def build_dev_jar_download_embed(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    previous_size_bytes: int | None = None,
) -> discord.Embed:
    """Convenience wrapper returning just the primary embed (single-embed case)."""
    embeds, _ = build_dev_jar_download_embeds(
        artifact,
        commits=commits,
        sha256=sha256,
        workflow_run_url=workflow_run_url,
        previous_size_bytes=previous_size_bytes,
    )
    return embeds[0]


def build_dev_jar_review_embeds(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    status: str = "Pending review",
    actor: str | None = None,
) -> tuple[list[discord.Embed], str | None]:
    """Build the staff review embed(s) posted to the dev jar review channel."""
    description = (
        "A push has been detected on GitHub and the dev jar built and uploaded successfully. "
        "Review the accumulated commits below, then **Publish** to announce it to the download "
        "channels, or **Discard** to drop this build without publishing."
    )
    base_fields = _build_base_dev_jar_fields(artifact, sha256=sha256, previous_size_bytes=None)
    status_field = ("Status", status)
    commit_count_field = ("Commits since last decision", str(len(commits)))
    base_char_count = (
        len("DragonMineZ Dev Jar Review")
        + len(description)
        + len(status_field[0]) + len(status_field[1])
        + len(commit_count_field[0]) + len(commit_count_field[1])
        + sum(len(name) + len(value) for name, value, _ in base_fields)
    )
    layout = build_dev_jar_commit_layout(commits, base_char_count=base_char_count)

    primary = discord.Embed(
        title="DragonMineZ Dev Jar Review",
        description=description,
        url=workflow_run_url,
        colour=DEV_JAR_REVIEW_EMBED_COLOR,
    )
    primary.add_field(name=status_field[0], value=status_field[1], inline=True)
    primary.add_field(name=commit_count_field[0], value=commit_count_field[1], inline=True)
    for name, value, inline in base_fields:
        primary.add_field(name=name, value=value, inline=inline)

    embeds = [primary]
    _append_fields_across_embeds(embeds, layout.fields, colour=DEV_JAR_REVIEW_EMBED_COLOR)

    if actor:
        embeds[-1].set_footer(text=actor)

    overflow_text = layout.full_changelog_text if layout.overflowed else None
    return embeds, overflow_text


class DevJarDownloadView(discord.ui.View):
    def __init__(
        self,
        artifact: DevJarArtifact,
        *,
        patch_notes_url: str,
        is_manual: bool = False,
    ):
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
                url=patch_notes_url,
            )
        )


class DevJarReviewView(discord.ui.View):
    """Persistent Publish/Discard prompt.

    The buttons carry stable custom_ids and hold no per-message state, so they
    keep working after a bot restart: clicks are routed through the cog's
    on_interaction listener, which reloads the pending review from the database
    (there is only ever one, a singleton row) before acting.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Publish",
                style=discord.ButtonStyle.success,
                custom_id=DEV_JAR_REVIEW_PUBLISH_ID,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Discard",
                style=discord.ButtonStyle.danger,
                custom_id=DEV_JAR_REVIEW_DISCARD_ID,
            )
        )


class DevJarDownloadsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.token_store = OneTimeDownloadTokenStore(now=time.time)
        self._release_webhook_route_registered = False
        self._release_get_routes_registered = False
        self._pending_review_lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._register_release_webhook_route()
        self._register_release_get_routes()

    def cog_unload(self) -> None:
        unregister_extra_webhook_route(self.settings.dev_jar_download_webhook_path)
        unregister_extra_get_route(f"{self.settings.dev_jar_download_download_path.rstrip('/')}/")
        unregister_extra_get_route(PROTECTED_ARTIFACTS_PATH)

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
        register_extra_get_route(
            path_prefix=PROTECTED_ARTIFACTS_PATH,
            handle_request=self._handle_protected_artifacts_request,
        )

    def _handle_protected_artifacts_request(
        self, path: str, query: dict[str, list[str]]
    ) -> ReleaseWebhookHttpResponse:
        loop = getattr(getattr(self, "bot", None), "loop", None)
        if loop is None or not loop.is_running():
            return text_http_response(503, "Bot event loop is not ready")
        future = asyncio.run_coroutine_threadsafe(self._collect_protected_artifact_names(), loop)
        try:
            names = future.result(timeout=5)
        except Exception:
            log.exception("Failed to collect protected dev jar artifact names")
            return text_http_response(500, "Failed to collect protected artifacts")
        body = json.dumps({"protected": names}).encode("utf-8")
        return ReleaseWebhookHttpResponse(status=200, body=body, content_type="application/json")

    async def _collect_protected_artifact_names(self) -> list[str]:
        names: set[str] = set()
        published = await get_published_dev_jar_file_name()
        if published:
            names.add(published)
        pending = await get_pending_dev_jar_review()
        if pending is not None:
            names.add(pending.artifact.file_name)
        return sorted(names)

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

    async def _resolve_review_channel(self) -> discord.abc.Messageable:
        channel_id = self.settings.dev_jar_review_channel_id
        if channel_id is None:
            raise RuntimeError("dev_jar_review_channel_id is not configured")
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError(f"Configured dev jar review channel {channel_id} is not messageable")
        return channel

    def _overflow_files(self, overflow_text: str | None) -> list[discord.File]:
        if not overflow_text:
            return []
        return [discord.File(io.BytesIO(overflow_text.encode("utf-8")), filename=OVERFLOW_COMMITS_FILENAME)]

    def _patch_notes_url(self) -> str:
        return build_patch_notes_url(
            self.settings.patch_notes_repo,
            self.settings.patch_notes_branch,
            self.settings.patch_notes_file_path,
        )

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
        embeds, overflow_text = build_dev_jar_download_embeds(
            artifact,
            commits=commits,
            sha256=sha256,
            workflow_run_url=workflow_run_url,
            previous_size_bytes=previous_size_bytes,
        )
        patch_notes_url = self._patch_notes_url()
        target_channels = [channel] if channel is not None else await self._resolve_announcement_channels()
        for target_channel in target_channels:
            await target_channel.send(
                embeds=embeds,
                view=DevJarDownloadView(
                    artifact, patch_notes_url=patch_notes_url, is_manual=is_manual
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                files=self._overflow_files(overflow_text),
            )
        # Record this as the live public download so the VPS-side cleanup script
        # (via the /dmz-dev-jar/protected endpoint) never prunes it out from under
        # the download button we just posted.
        await set_published_dev_jar_file_name(artifact.file_name)

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
        await self._queue_for_review(
            artifact=artifact,
            commits=payload.commits,
            sha256=payload.sha256,
            workflow_run_url=payload.workflow_run_url,
        )

    async def _queue_for_review(
        self,
        *,
        artifact: DevJarArtifact,
        commits: tuple[DevJarCommit, ...],
        sha256: str | None,
        workflow_run_url: str | None,
    ) -> None:
        if self.settings.dev_jar_review_channel_id is None:
            log.error(
                "dev_jar_review_channel_id is not configured; publishing dev jar directly "
                "without staff review."
            )
            await self._post_download_announcement(
                artifact,
                commits=commits,
                sha256=sha256,
                workflow_run_url=workflow_run_url,
                previous_size_bytes=self._get_previous_artifact_size(artifact.file_name),
            )
            return

        async with self._pending_review_lock:
            existing = await get_pending_dev_jar_review()
            merged_commits = merge_dev_jar_commits(
                existing.commits if existing is not None else (),
                commits,
            )
            # Persist the merged commit cache up front (still pointing at the old
            # message, if any) so a later Discord failure can never drop commits;
            # the message link is refreshed after the fresh post below.
            await upsert_pending_dev_jar_review(
                artifact=artifact,
                sha256=sha256,
                workflow_run_url=workflow_run_url,
                commits=merged_commits,
                channel_id=existing.channel_id if existing is not None else None,
                message_id=existing.message_id if existing is not None else None,
            )

            channel = await self._resolve_review_channel()

            # Repost fresh: delete the previous pending prompt (if any) and post a
            # new one so it lands at the bottom of the channel and is actually
            # noticed, instead of silently editing a message scrolled out of view.
            if existing is not None and existing.message_id is not None:
                await self._delete_review_message(channel, existing.message_id)

            embeds, overflow_text = build_dev_jar_review_embeds(
                artifact,
                commits=merged_commits,
                sha256=sha256,
                workflow_run_url=workflow_run_url,
            )
            view = DevJarReviewView()
            files = self._overflow_files(overflow_text)

            sent = await channel.send(
                embeds=embeds,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                files=files,
            )
            await set_pending_dev_jar_review_message(channel.id, sent.id)

    async def _delete_review_message(
        self, channel: discord.abc.Messageable, message_id: int
    ) -> None:
        """Best-effort delete of a prior pending review message."""
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return
        except discord.HTTPException:
            log.exception(
                "Failed to fetch prior dev jar review message %s for deletion", message_id
            )
            return
        try:
            await message.delete()
        except discord.HTTPException:
            log.exception("Failed to delete prior dev jar review message %s", message_id)

    def _delete_artifact_file(self, artifact: DevJarArtifact) -> None:
        """Best-effort delete of a dev jar file from the upload directory."""
        try:
            path = artifact.resolve_path(self._upload_dir())
        except Exception:
            log.exception(
                "Failed to resolve discarded dev jar path for %s", artifact.file_name
            )
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.exception("Failed to delete discarded dev jar %s", artifact.file_name)

    async def _review_still_current(self, review: PendingDevJarReview) -> bool:
        """True if the pending review still matches this build (not already
        published/discarded, nor replaced by a newer push). Guards against a
        second button click racing the first."""
        current = await get_pending_dev_jar_review()
        return current is not None and current.artifact.file_name == review.artifact.file_name

    async def _publish_pending_review(
        self,
        interaction: discord.Interaction,
        review: PendingDevJarReview,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self._pending_review_lock:
            if not await self._review_still_current(review):
                await interaction.followup.send(
                    "This dev jar review was already handled or replaced by a newer push.",
                    ephemeral=True,
                )
                return
            try:
                path = self._artifact_path(review.artifact)
                stat = path.stat()
                artifact = DevJarArtifact(
                    file_name=review.artifact.file_name,
                    version=review.artifact.version,
                    commit_sha=review.artifact.commit_sha,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            except FileNotFoundError:
                await interaction.followup.send(
                    f"Cannot publish: `{review.artifact.file_name}` is no longer on disk.",
                    ephemeral=True,
                )
                return

            await self._post_download_announcement(
                artifact,
                commits=review.commits,
                sha256=review.sha256,
                workflow_run_url=review.workflow_run_url,
                previous_size_bytes=self._get_previous_artifact_size(artifact.file_name),
            )
            await clear_pending_dev_jar_review()

        if interaction.message is not None:
            embeds, _ = build_dev_jar_review_embeds(
                artifact,
                commits=review.commits,
                sha256=review.sha256,
                workflow_run_url=review.workflow_run_url,
                status="Published",
                actor=f"Published by {interaction.user}",
            )
            await interaction.message.edit(embeds=embeds, view=None, attachments=[])
        await interaction.followup.send(
            f"DragonMineZ dev jar `{artifact.file_name}` published.",
            ephemeral=True,
        )

    async def _discard_pending_review(
        self,
        interaction: discord.Interaction,
        review: PendingDevJarReview,
    ) -> None:
        # Discard rejects *this* build: the jar is deleted from disk (only the
        # published and next-pending jars are worth keeping as backups), but the
        # accumulated commits stay cached (see dev_jar_pending_review) so they
        # carry forward and reappear on the next push's prompt instead of being
        # lost. The message link is cleared so that next push posts a fresh
        # prompt rather than trying to re-edit this discard record.
        await interaction.response.defer(ephemeral=True)
        async with self._pending_review_lock:
            if not await self._review_still_current(review):
                await interaction.followup.send(
                    "This dev jar review was already handled or replaced by a newer push.",
                    ephemeral=True,
                )
                return
            self._delete_artifact_file(review.artifact)
            if interaction.message is not None:
                embeds, _ = build_dev_jar_review_embeds(
                    review.artifact,
                    commits=review.commits,
                    sha256=review.sha256,
                    workflow_run_url=review.workflow_run_url,
                    status="Discarded",
                    actor=f"Discarded by {interaction.user}",
                )
                await interaction.message.edit(embeds=embeds, view=None, attachments=[])
            await clear_pending_dev_jar_review_message()
        await interaction.followup.send(
            "Dev jar build discarded and deleted from disk. Accumulated commits "
            "remain queued for the next push.",
            ephemeral=True,
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
        if custom_id == DEV_JAR_REVIEW_PUBLISH_ID:
            await self._handle_review_decision(interaction, publish=True)
        elif custom_id == DEV_JAR_REVIEW_DISCARD_ID:
            await self._handle_review_decision(interaction, publish=False)
        elif custom_id.startswith(MANUAL_DOWNLOAD_BUTTON_PREFIX):
            await self._handle_download_button(
                interaction,
                custom_id.removeprefix(MANUAL_DOWNLOAD_BUTTON_PREFIX),
                is_manual=True,
            )
        elif custom_id.startswith(DOWNLOAD_BUTTON_PREFIX):
            await self._handle_download_button(interaction, custom_id.removeprefix(DOWNLOAD_BUTTON_PREFIX))

    async def _handle_review_decision(
        self, interaction: discord.Interaction, *, publish: bool
    ) -> None:
        if not can_post_download_announcement(
            interaction.user,
            staff_role_ids=tuple(self.settings.discord_staff_role_ids),
        ):
            await interaction.response.send_message(
                "Only staff can publish or discard dev jar builds.",
                ephemeral=True,
            )
            return
        # State lives in the DB (singleton row), not the View instance, so this
        # works even after a restart wiped the in-memory view.
        review = await get_pending_dev_jar_review()
        if review is None:
            await interaction.response.send_message(
                "This dev jar review is no longer pending (already published or discarded).",
                ephemeral=True,
            )
            return
        if publish:
            await self._publish_pending_review(interaction, review)
        else:
            await self._discard_pending_review(interaction, review)

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
