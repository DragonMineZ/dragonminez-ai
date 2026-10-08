import asyncio
import html
import io
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
from bulmaai.services import build_gate
from bulmaai.services.dev_jar_downloads import (
    DevJarArtifact,
    DevJarCommit,
    DevJarDownloadClaim,
    DevJarUploadPayload,
    OneTimeDownloadTokenStore,
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
from bulmaai.ui.dev_jar_views import (
    COMMITS_FILENAME,
    artifact_day,
    build_dev_jar_download_view,
    build_dev_jar_review_view,
)
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import has_any_allowed_role, is_admin


log = logging.getLogger(__name__)

DOWNLOAD_BUTTON_PREFIX = "dev_jar_download:"
MANUAL_DOWNLOAD_BUTTON_PREFIX = "dev_jar_download:manual:"
DOWNLOAD_FILE_SUFFIX = "/file"
# Stable custom_ids for the staff review prompt so its buttons survive restarts.
DEV_JAR_REVIEW_PUBLISH_ID = "dev_jar_review:publish"
DEV_JAR_REVIEW_DISCARD_ID = "dev_jar_review:discard"
# Bound to 0.0.0.0 (see config.py RELEASE_WEBHOOK_HOST) and gated behind the same
# X-DMZ-Release-Bot-Secret header as the dev jar upload webhook. The VPS-side
# cleanup script polls it to learn which dev jar files must survive pruning: the
# currently published build (its public download button is live) and the
# currently pending-review build (staff haven't decided on it yet).
PROTECTED_ARTIFACTS_PATH = "/dmz-dev-jar/protected"


def can_post_download_announcement(member: object, *, staff_role_ids: tuple[int, ...]) -> bool:
    return is_admin(member) or has_any_allowed_role(member, staff_role_ids)  # type: ignore[arg-type]


def can_download_dev_jar(
    member: object,
    *,
    patreon_role_ids: tuple[int, ...],
    tester_role_ids: tuple[int, ...],
) -> bool:
    return (
        is_admin(member)  # type: ignore[arg-type]
        or has_any_allowed_role(member, patreon_role_ids)  # type: ignore[arg-type]
        or has_any_allowed_role(member, tester_role_ids)  # type: ignore[arg-type]
    )


def download_actions(artifact: DevJarArtifact, *, patch_notes_url: str, is_manual: bool = False) -> discord.ui.ActionRow:
    prefix = MANUAL_DOWNLOAD_BUTTON_PREFIX if is_manual else DOWNLOAD_BUTTON_PREFIX
    return discord.ui.ActionRow(
        discord.ui.Button(label="Get download link", style=discord.ButtonStyle.primary, custom_id=f"{prefix}{artifact.file_name}"),
        discord.ui.Button(label=f"Patch Notes – {artifact_day(artifact)}", url=patch_notes_url),
    )


def review_actions() -> discord.ui.ActionRow:
    """Publish/Discard. Stable custom ids and no per-message state: clicks are routed through the cog's
    on_interaction listener, which reloads the pending review (a singleton row) before acting."""
    return discord.ui.ActionRow(
        discord.ui.Button(label="Publish", style=discord.ButtonStyle.success, custom_id=DEV_JAR_REVIEW_PUBLISH_ID),
        discord.ui.Button(label="Discard", style=discord.ButtonStyle.danger, custom_id=DEV_JAR_REVIEW_DISCARD_ID),
    )


class DevJarDownloadsCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.token_store = OneTimeDownloadTokenStore(now=time.time)
        self._webhook_path: str | None = None
        self._get_paths: list[str] = []
        self._pending_review_lock = asyncio.Lock()

    @property
    def settings(self):
        return self.__dict__.get("_settings_override") or self.bot.settings

    @settings.setter
    def settings(self, value) -> None:
        # Tests build the cog without a bot and pin settings directly.
        self.__dict__["_settings_override"] = value

    async def on_startup(self) -> None:
        self._register_release_webhook_route()
        self._register_release_get_routes()

    async def on_shutdown(self) -> None:
        if self._webhook_path is not None:
            unregister_extra_webhook_route(self._webhook_path)
            self._webhook_path = None
        for path_prefix in self._get_paths:
            unregister_extra_get_route(path_prefix)
        self._get_paths = []

    def export_state(self) -> dict | None:
        # Download links already handed out must keep working after a reload.
        self.token_store.cleanup_expired()
        if not self.token_store._grants:
            return None
        return {"grants": dict(self.token_store._grants), "claimed": set(self.token_store._claimed)}

    def import_state(self, state: dict) -> None:
        self.token_store._grants.update(state["grants"])
        self.token_store._claimed |= state["claimed"]

    def _register_release_webhook_route(self) -> None:
        if self._webhook_path is not None:
            return
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

        self._webhook_path = self.settings.dev_jar_download_webhook_path
        register_extra_webhook_route(
            path=self._webhook_path,
            secret=self.settings.release_webhook_secret,
            secret_header="X-DMZ-Release-Bot-Secret",
            parse_payload=parse_dev_jar_upload_payload,
            submit_payload=submit_payload,
            accepted_body="Dev jar upload queued",
        )

    def _register_release_get_routes(self) -> None:
        if self._get_paths:
            return
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
        self._get_paths.append(direct_prefix)
        if not self.settings.release_webhook_secret:
            log.error(
                "DMZ_RELEASE_BOT_WEBHOOK_SECRET is missing; dev jar protected-artifacts route skipped."
            )
            return
        register_extra_get_route(
            path_prefix=PROTECTED_ARTIFACTS_PATH,
            handle_request=self._handle_protected_artifacts_request,
            secret=self.settings.release_webhook_secret,
            secret_header="X-DMZ-Release-Bot-Secret",
        )
        self._get_paths.append(PROTECTED_ARTIFACTS_PATH)

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

    async def _resolve_messageable_channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError(f"Configured dev jar channel {channel_id} is not messageable")
        return channel

    async def _resolve_channel(self) -> discord.abc.Messageable:
        channel_id = self.settings.dev_jar_download_channel_id
        if channel_id is None:
            raise RuntimeError("dev_jar_download_channel_id is not configured")
        return await self._resolve_messageable_channel(channel_id)

    async def _resolve_announcement_channels(self) -> list[discord.abc.Messageable]:
        return [
            await self._resolve_messageable_channel(channel_id)
            for channel_id in self.settings.dev_jar_announcement_channel_ids
        ]

    async def _resolve_review_channel(self) -> discord.abc.Messageable:
        channel_id = self.settings.dev_jar_review_channel_id
        if channel_id is None:
            raise RuntimeError("dev_jar_review_channel_id is not configured")
        return await self._resolve_messageable_channel(channel_id)

    def _commit_list_files(self, commit_list_text: str | None) -> list[discord.File]:
        if not commit_list_text:
            return []
        return [discord.File(io.BytesIO(commit_list_text.encode("utf-8")), filename=COMMITS_FILENAME)]

    def _patch_notes_url(self) -> str:
        return build_patch_notes_url(
            self.settings.patch_notes_repo,
            self.settings.patch_notes_branch,
            self.settings.patch_notes_file_path,
        )

    async def _create_feedback_thread(
        self, message: discord.Message | None, artifact: DevJarArtifact
    ) -> None:
        """Best-effort per-build feedback thread on the announcement message.
        Crash logs posted in it are already auto-parsed by LogParserCog. The
        announcement has already gone out, so a missing permission or a
        thread limit must not raise."""
        if message is None:
            return
        try:
            await message.create_thread(name=f"Build {artifact.version}")
        except discord.HTTPException:
            log.exception(
                "Failed to create feedback thread for dev jar build %s", artifact.version
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
        changelog: str | None = None,
    ) -> None:
        view, commit_list_text = build_dev_jar_download_view(
            artifact,
            commits=commits,
            actions=download_actions(artifact, patch_notes_url=self._patch_notes_url(), is_manual=is_manual),
            changelog=changelog,
            sha256=sha256,
            workflow_run_url=workflow_run_url,
            previous_size_bytes=previous_size_bytes,
        )
        target_channels = [channel] if channel is not None else await self._resolve_announcement_channels()
        for target_channel in target_channels:
            sent = await target_channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                files=self._commit_list_files(commit_list_text),
            )
            await self._create_feedback_thread(sent, artifact)
        # Record this as the live public download so the VPS-side cleanup script
        # (via the /dmz-dev-jar/protected endpoint) never prunes it out from under
        # the download button we just posted.
        await set_published_dev_jar_file_name(artifact.file_name)

    def _refresh_artifact_stat(self, artifact: DevJarArtifact) -> DevJarArtifact:
        """Rebuild `artifact` with fresh size/mtime read from disk.

        Raises FileNotFoundError (via `_artifact_path`) if the file is gone.
        """
        path = self._artifact_path(artifact)
        stat = path.stat()
        return DevJarArtifact(
            file_name=artifact.file_name,
            version=artifact.version,
            commit_sha=artifact.commit_sha,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    async def _handle_upload_payload(self, payload: DevJarUploadPayload) -> None:
        artifact = self._refresh_artifact_stat(payload.artifact)
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
        async with self._pending_review_lock:
            existing = await get_pending_dev_jar_review()
            merged_commits = merge_dev_jar_commits(
                existing.commits if existing is not None else (),
                commits,
            )
            # Persist the merged commit cache up front (still pointing at the old
            # message, if any) so a later Discord failure can never drop commits;
            # the message link is refreshed after the fresh post below. This also
            # keeps the build's artifact + commits alive (and the jar protected
            # from VPS pruning) even if there's nowhere to post the review below.
            await upsert_pending_dev_jar_review(
                artifact=artifact,
                sha256=sha256,
                workflow_run_url=workflow_run_url,
                commits=merged_commits,
                channel_id=existing.channel_id if existing is not None else None,
                message_id=existing.message_id if existing is not None else None,
            )

            if self.settings.dev_jar_review_channel_id is None:
                # No staff review gate configured: refuse to publish rather than
                # skip straight to the public channels. The build stays queued
                # (persisted above) so nothing is lost once a review channel is
                # set. log.error is already forwarded to Discord (see
                # discord_log_forwarding.py), so this doubles as the staff alert.
                log.error(
                    "dev_jar_review_channel_id is not configured; refusing to publish dev jar "
                    "%s to the public channels. %d commit(s) remain queued for staff review.",
                    artifact.file_name,
                    len(merged_commits),
                )
                return

            channel = await self._resolve_review_channel()

            # Repost fresh: delete the previous pending prompt (if any) and post a
            # new one so it lands at the bottom of the channel and is actually
            # noticed, instead of silently editing a message scrolled out of view.
            if existing is not None and existing.message_id is not None:
                await self._delete_review_message(channel, existing.message_id)

            view, overflow_text = build_dev_jar_review_view(
                artifact,
                commits=merged_commits,
                sha256=sha256,
                workflow_run_url=workflow_run_url,
                actions=review_actions(),
            )
            files = self._commit_list_files(overflow_text)

            sent = await channel.send(
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

    def _delete_artifact_file(self, artifact: DevJarArtifact) -> bool:
        """Best-effort delete of a dev jar file from the upload directory.

        Returns True if the file is gone (deleted or already absent), False if
        deletion failed (e.g. the bot user lacks write access to the upload
        directory) so callers can report the outcome truthfully.
        """
        try:
            path = artifact.resolve_path(self._upload_dir())
        except Exception:
            log.exception(
                "Failed to resolve discarded dev jar path for %s", artifact.file_name
            )
            return False
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            log.exception("Failed to delete discarded dev jar %s", artifact.file_name)
            return False

    async def _review_still_current(self, review: PendingDevJarReview) -> bool:
        """True if the pending review still matches this build (not already
        published/discarded, nor replaced by a newer push). Guards against a
        second button click racing the first."""
        current = await get_pending_dev_jar_review()
        return current is not None and current.artifact.file_name == review.artifact.file_name

    async def _staff_changelog(self, artifact: DevJarArtifact) -> str | None:
        try:
            return await build_gate.latest_build_changelog()
        except Exception:
            log.exception("Failed to load the changelog for dev jar %s; publishing without one", artifact.file_name)
            return None

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
                artifact = self._refresh_artifact_stat(review.artifact)
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
                changelog=await self._staff_changelog(artifact),
            )
            await clear_pending_dev_jar_review()

        if interaction.message is not None:
            view, _ = build_dev_jar_review_view(
                artifact,
                commits=review.commits,
                sha256=review.sha256,
                workflow_run_url=review.workflow_run_url,
                status="Published",
                actor=f"Published by {interaction.user}",
            )
            await interaction.message.edit(view=view, attachments=[])
        await interaction.followup.send(
            f"📦 DragonMineZ dev jar `{artifact.file_name}` published by {interaction.user.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
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
            deleted = self._delete_artifact_file(review.artifact)
            if interaction.message is not None:
                view, _ = build_dev_jar_review_view(
                    review.artifact,
                    commits=review.commits,
                    sha256=review.sha256,
                    workflow_run_url=review.workflow_run_url,
                    status="Discarded",
                    actor=f"Discarded by {interaction.user}",
                )
                await interaction.message.edit(view=view, attachments=[])
            await clear_pending_dev_jar_review_message()
        if deleted:
            outcome = "and deleted from disk"
        else:
            outcome = (
                "but the .jar could not be deleted from disk (check the bot logs / "
                "upload-directory permissions)"
            )
        await interaction.followup.send(
            f"🗑️ Dev jar build discarded by {interaction.user.mention} {outcome}. Accumulated commits remain queued "
            "for the next push.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
            if not can_download_dev_jar(
                interaction.user,
                patreon_role_ids=tuple(self.settings.dev_jar_patreon_role_ids),
                tester_role_ids=tuple(self.settings.dev_jar_tester_role_ids),
            ):
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
