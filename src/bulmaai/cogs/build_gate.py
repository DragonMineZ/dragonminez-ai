"""Build gate: a push that changes Java asks staff-devs before the dev-jar workflow runs.

Flow: GitHub push webhook -> prompt (Build jar / Skip, auto-closes after build_gate_expire_minutes) -> on
approval the bot dispatches the mod repo's dev-jar workflow and live-edits the message with the run's
progress.
ponytail: all state lives in build_requests (services/build_gate.py), so prompts expire and running builds
keep being tracked across a bot restart or reload; nothing is held only in memory except the poll tasks,
which on_startup re-creates from the 'building' rows."""

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services import build_gate
from bulmaai.services.release_webhook import (
    ReleaseWebhookHttpResponse,
    register_extra_raw_webhook_route,
    text_http_response,
    unregister_extra_raw_webhook_route,
)
from bulmaai.ui.build_gate_views import (
    APPROVE_PREFIX,
    CHANGELOG_PREFIX,
    REJECT_PREFIX,
    ChangelogModal,
    final_embed,
    gate_embed,
    gate_view,
    preview_embeds,
    preview_view,
    progress_embed,
)
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import is_admin

log = logging.getLogger(__name__)

POLL_SECONDS = 5
FIND_RUN_ATTEMPTS = 36  # 36 x 5s: the dispatched run normally shows up within seconds
TRACK_TIMEOUT_SECONDS = 45 * 60
MAX_POLL_FAILURES = 5
NO_PINGS = discord.AllowedMentions.none()


class BuildGateCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.repo_full_name = f"{self.settings.GITHUB_OWNER}/{self.settings.GITHUB_DEFAULT_REPO}"
        self.github = GitHubService(
            auth=GitHubAppAuth(
                app_id=self.settings.GH_APP_ID,
                installation_id=self.settings.GH_INSTALLATION_ID,
                private_key_pem=self.settings.GH_APP_PRIVATE_KEY_PEM,
            ),
            owner=self.settings.GITHUB_OWNER,
            repo=self.settings.GITHUB_DEFAULT_REPO,
            base_branch=self.settings.GITHUB_BASE_BRANCH,
        )
        self._lock = asyncio.Lock()
        self._changelog_lock = asyncio.Lock()
        self._trackers: dict[int, asyncio.Task] = {}
        self._route_path: str | None = None

    @property
    def settings(self):
        return self.__dict__.get("_settings_override") or self.bot.settings

    @settings.setter
    def settings(self, value) -> None:
        # Tests build the cog without a bot and pin settings directly.
        self.__dict__["_settings_override"] = value

    # --- lifecycle ------------------------------------------------------------------------------

    async def on_startup(self) -> None:
        self._register_webhook_route()
        if not self.expire_prompts.is_running():
            self.expire_prompts.start()
        # Trackers resume from the 'building' rows, so nothing needs carrying across a reload.
        for request in await build_gate.with_status(build_gate.BUILDING):
            self._start_tracker(request.id)

    async def on_shutdown(self) -> None:
        if self._route_path is not None:
            unregister_extra_raw_webhook_route(self._route_path)
            self._route_path = None
        self.expire_prompts.cancel()
        for task in list(self._trackers.values()):
            task.cancel()

    def _channel_id(self) -> int | None:
        return self.settings.build_gate_channel_id or self.settings.dev_jar_review_channel_id

    async def _channel(self, channel_id: int | None = None):
        channel_id = channel_id or self._channel_id()
        if channel_id is None:
            return None
        return self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)

    # --- GitHub push webhook --------------------------------------------------------------------

    def _register_webhook_route(self) -> None:
        if self._route_path is not None or not self.settings.build_gate_enabled:
            return
        secret = self.settings.github_push_webhook_secret
        if not secret:
            log.error("GITHUB_PUSH_WEBHOOK_SECRET is missing; build gate push webhook skipped.")
            return
        loop = asyncio.get_running_loop()

        def handle(body: bytes, headers) -> ReleaseWebhookHttpResponse:
            if not build_gate.verify_signature(secret, body, headers.get("X-Hub-Signature-256")):
                return text_http_response(401, "Bad signature")
            event = headers.get("X-GitHub-Event")
            if event == "ping":
                return text_http_response(200, "pong")
            if event != "push":
                return text_http_response(202, "Ignored")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return text_http_response(400, "Invalid JSON body")
            push = build_gate.parse_push(
                payload, repo_full_name=self.repo_full_name, ignore_branches=(self.settings.GITHUB_BASE_BRANCH,)
            )
            if push is None:
                return text_http_response(202, "Ignored: no Java changes on a feature branch")
            future = asyncio.run_coroutine_threadsafe(self._handle_push(push), loop)

            def log_result(done: "asyncio.Future[None]") -> None:
                try:
                    done.result()
                except Exception:
                    log.exception("Build gate push handling failed")

            future.add_done_callback(log_result)
            return text_http_response(202, "Build prompt queued")

        self._route_path = self.settings.build_gate_webhook_path
        register_extra_raw_webhook_route(path=self._route_path, handle_request=handle)

    async def _handle_push(self, push: build_gate.PushInfo) -> None:
        async with self._lock:
            now = discord.utils.utcnow()
            window = self.settings.build_gate_window_minutes
            recent = await build_gate.recent(self.repo_full_name, now - timedelta(minutes=window), exclude_id=0)
            pending_same_branch = sorted(
                (r for r in recent if r.branch == push.branch and r.status == build_gate.PENDING), key=lambda r: r.id
            )
            commits = push.commits
            changelog = None
            for earlier in pending_same_branch:
                commits = build_gate.merge_commits(earlier.commits, commits)
                changelog = earlier.changelog or changelog

            request = await build_gate.create(
                repo=self.repo_full_name,
                branch=push.branch,
                head_sha=push.head_sha,
                pusher=push.pusher,
                commits=commits,
                source="push",
                expires_at=now + timedelta(minutes=self.settings.build_gate_expire_minutes),
                changelog=changelog,
            )

            replaced: set[int] = set()
            for earlier in pending_same_branch:
                if await build_gate.transition(earlier.id, build_gate.SUPERSEDED, from_status=build_gate.PENDING):
                    replaced.add(earlier.id)
                    await self._edit_message(
                        earlier, final_embed(earlier, status=build_gate.SUPERSEDED, note=f"Folded into prompt #{request.id}")
                    )
            others = [replace(r, status=build_gate.SUPERSEDED) if r.id in replaced else r for r in recent]

            channel = await self._channel()
            if channel is None:
                log.error("build gate channel is not configured; push %s on %s has no prompt.", push.head_sha[:7], push.branch)
                return
            message = await channel.send(
                embed=gate_embed(request, recent=others, window_minutes=window),
                view=gate_view(request),
                allowed_mentions=NO_PINGS,
            )
            await build_gate.set_message(request.id, channel.id, message.id)

    # --- prompt expiry (durable sweep) ----------------------------------------------------------

    @tasks.loop(minutes=1)
    async def expire_prompts(self) -> None:
        try:
            due = await build_gate.due(discord.utils.utcnow())
        except Exception:
            log.exception("Couldn't load due build prompts")
            return
        for request in due:
            if await build_gate.transition(request.id, build_gate.EXPIRED, from_status=build_gate.PENDING):
                await self._edit_message(request, final_embed(request, status=build_gate.EXPIRED))

    @expire_prompts.before_loop
    async def _before_expire_prompts(self) -> None:
        await self.bot.wait_until_ready()

    # --- buttons --------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not isinstance(custom_id, str):
            return
        if custom_id.startswith(CHANGELOG_PREFIX) and custom_id.removeprefix(CHANGELOG_PREFIX).isdigit():
            await self._handle_changelog_button(interaction, int(custom_id.removeprefix(CHANGELOG_PREFIX)))
            return
        for prefix, approve in ((APPROVE_PREFIX, True), (REJECT_PREFIX, False)):
            if custom_id.startswith(prefix) and custom_id.removeprefix(prefix).isdigit():
                await self._handle_decision(interaction, int(custom_id.removeprefix(prefix)), approve=approve)
                return

    async def _handle_decision(self, interaction: discord.Interaction, request_id: int, *, approve: bool) -> None:
        if not is_admin(interaction.user):
            await interaction.response.send_message("Only administrators can answer build prompts.", ephemeral=True)
            return
        await interaction.response.defer()
        async with self._lock:
            request = await build_gate.get(request_id)
            if request is None or request.status != build_gate.PENDING:
                await interaction.followup.send("This prompt was already handled, replaced or closed.", ephemeral=True)
                return
            if not approve:
                rejected = await build_gate.transition(
                    request_id, build_gate.REJECTED, from_status=build_gate.PENDING, by=interaction.user.id
                )
                if rejected:
                    await interaction.edit_original_response(
                        embed=final_embed(rejected, status=build_gate.REJECTED, note=f"Skipped by {interaction.user.display_name}"),
                        view=None,
                    )
                return
            busy = await self._active_build()
            if busy is not None:
                await interaction.followup.send(
                    f"A build is already running (#{busy.id} on **{busy.branch}**). Wait for it to finish, then click again.",
                    ephemeral=True,
                )
                return
            error = await self._start_build(request, interaction.user.id)
        if error:
            await interaction.followup.send(error, ephemeral=True)

    # --- changelog ------------------------------------------------------------------------------

    async def _handle_changelog_button(self, interaction: discord.Interaction, request_id: int) -> None:
        if not is_admin(interaction.user):
            await interaction.response.send_message("Only administrators can edit the changelog.", ephemeral=True)
            return
        request = await build_gate.get(request_id)
        if request is None or request.status not in (build_gate.PENDING, build_gate.BUILDING):
            await interaction.response.send_message("The build already finished, so its changelog is locked.", ephemeral=True)
            return
        await interaction.response.send_modal(ChangelogModal(request, self._save_changelog))

    async def _save_changelog(self, interaction: discord.Interaction, request_id: int, text: str) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self._changelog_lock:
            request = await build_gate.set_changelog(request_id, text)
            if request is None:
                await interaction.followup.send("The build already finished, so the changelog is locked.", ephemeral=True)
                return
            if request.status == build_gate.PENDING:
                await self._refresh_gate_message(request)
            else:
                await self._edit_preview(request, locked=False)
        saved = "Changelog saved." if request.changelog else "Changelog cleared. The public post will have no What's New."
        await interaction.followup.send(saved, ephemeral=True)

    async def _refresh_gate_message(self, request: build_gate.BuildRequest) -> None:
        if request.channel_id is None or request.message_id is None:
            return
        window = self.settings.build_gate_window_minutes
        since = discord.utils.utcnow() - timedelta(minutes=window)
        recent = await build_gate.recent(self.repo_full_name, since, exclude_id=request.id)
        await self._edit_message(
            request, gate_embed(request, recent=recent, window_minutes=window), view=gate_view(request)
        )

    async def _post_preview(self, request: build_gate.BuildRequest) -> None:
        if request.channel_id is None:
            return
        try:
            channel = await self._channel(request.channel_id)
            message = await channel.send(
                embeds=preview_embeds(request), view=preview_view(request), allowed_mentions=NO_PINGS
            )
            await build_gate.set_preview_message(request.id, message.id)
        except discord.HTTPException:
            log.exception("Couldn't post the changelog preview for build %s", request.id)

    async def _edit_preview(self, request: build_gate.BuildRequest, *, locked: bool) -> None:
        if request.channel_id is None or request.preview_message_id is None:
            return
        try:
            channel = await self._channel(request.channel_id)
            await channel.get_partial_message(request.preview_message_id).edit(
                embeds=preview_embeds(request, locked=locked), view=None if locked else preview_view(request)
            )
        except discord.HTTPException:
            log.exception("Couldn't edit changelog preview %s", request.preview_message_id)

    async def _lock_preview(self, request_id: int) -> None:
        async with self._changelog_lock:
            request = await build_gate.get(request_id)
            if request is not None:
                await self._edit_preview(request, locked=True)

    # --- starting + tracking a build ------------------------------------------------------------

    async def _active_build(self) -> build_gate.BuildRequest | None:
        building = await build_gate.with_status(build_gate.BUILDING)
        return building[0] if building else None

    async def _start_build(self, request: build_gate.BuildRequest, approver_id: int) -> str | None:
        """Claims the request, dispatches the workflow and starts the progress tracker. Returns a user-facing
        error string on failure (the request is then marked failed), None on success. Caller holds self._lock."""
        claimed = await build_gate.transition(
            request.id, build_gate.BUILDING, from_status=build_gate.PENDING, by=approver_id
        )
        if claimed is None:
            return "This prompt was already handled."
        try:
            await self.github.dispatch_workflow(
                workflow=self.settings.build_gate_workflow,
                ref=self.settings.GITHUB_BASE_BRANCH,
                inputs={
                    "sha": claimed.head_sha,
                    "branch": claimed.branch,
                    "request_id": claimed.request_token,
                    "commits_json": build_gate.commits_input(claimed.commits),
                },
            )
        except Exception as error:
            log.exception("Dev jar workflow dispatch failed", extra={"event": "build_gate_dispatch_failed"})
            await build_gate.transition(claimed.id, build_gate.FAILED, from_status=build_gate.BUILDING)
            await self._edit_message(claimed, final_embed(claimed, status=build_gate.FAILED, note=f"Couldn't start the workflow: {error}"[:200]))
            return f"Couldn't start the workflow: {error}"
        await self._edit_message(claimed, progress_embed(claimed, job=None, run=None, approver_id=approver_id))
        async with self._changelog_lock:
            claimed = await build_gate.get(claimed.id) or claimed
            await self._post_preview(claimed)
        self._start_tracker(claimed.id)
        return None

    def _start_tracker(self, request_id: int) -> None:
        task = self._trackers.get(request_id)
        if task is not None and not task.done():
            return
        self._trackers[request_id] = asyncio.create_task(self._track(request_id))

    async def _track(self, request_id: int) -> None:
        try:
            await self._track_run(request_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Build progress tracking crashed", extra={"event": "build_gate_track_failed"})
        finally:
            self._trackers.pop(request_id, None)

    async def _fail(self, request: build_gate.BuildRequest, note: str) -> None:
        if await build_gate.transition(request.id, build_gate.FAILED, from_status=build_gate.BUILDING):
            await self._edit_message(request, final_embed(request, status=build_gate.FAILED, note=note))
            await self._lock_preview(request.id)

    async def _find_run(self, request: build_gate.BuildRequest) -> dict | None:
        marker = f"[{request.request_token}]"
        for _ in range(FIND_RUN_ATTEMPTS):
            try:
                runs = await self.github.list_workflow_runs(self.settings.build_gate_workflow)
            except Exception:
                log.exception("Listing workflow runs failed")
                runs = []
            for run in runs:
                if marker in (run.get("display_title") or run.get("name") or ""):
                    return run
            await asyncio.sleep(POLL_SECONDS)
        return None

    async def _track_run(self, request_id: int) -> None:
        request = await build_gate.get(request_id)
        if request is None or request.status != build_gate.BUILDING:
            return
        if request.run_id is None:
            run = await self._find_run(request)
            if run is None:
                await self._fail(request, "The workflow run never showed up on GitHub.")
                return
            await build_gate.set_run(request.id, run["id"], run["html_url"])
            request = replace(request, run_id=run["id"], run_url=run["html_url"])

        deadline = time.monotonic() + TRACK_TIMEOUT_SECONDS
        last_snapshot = None
        failures = 0
        while True:
            try:
                run = await self.github.get_workflow_run(request.run_id)
                jobs = await self.github.list_run_jobs(request.run_id)
                failures = 0
            except Exception:
                failures += 1
                log.exception("Polling the workflow run failed")
                if failures >= MAX_POLL_FAILURES:
                    await self._fail(request, "Lost contact with GitHub while tracking the build.")
                    return
                await asyncio.sleep(POLL_SECONDS)
                continue
            job = jobs[0] if jobs else None

            if run["status"] == "completed":
                ok = run.get("conclusion") == "success"
                status = build_gate.SUCCEEDED if ok else build_gate.FAILED
                if await build_gate.transition(request.id, status, from_status=build_gate.BUILDING):
                    note = None if ok else f"Run conclusion: {run.get('conclusion')}"
                    await self._edit_message(request, final_embed(request, status=status, job=job, note=note))
                    await self._lock_preview(request.id)
                return

            steps = build_gate.visible_steps(job)
            snapshot = (run["status"], tuple((s["name"], s["status"], s.get("conclusion")) for s in steps))
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                await self._edit_message(
                    request, progress_embed(request, job=job, run=run, approver_id=request.decided_by)
                )
            if time.monotonic() > deadline:
                await self._fail(request, "Stopped tracking after 45 minutes; check the run on GitHub.")
                return
            await asyncio.sleep(POLL_SECONDS)

    async def _edit_message(
        self, request: build_gate.BuildRequest, embed: discord.Embed, *, view: discord.ui.View | None = None
    ) -> None:
        if request.channel_id is None or request.message_id is None:
            return
        try:
            channel = await self._channel(request.channel_id)
            await channel.get_partial_message(request.message_id).edit(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Couldn't edit build gate message %s", request.message_id)


def setup(bot: discord.Bot):
    bot.add_cog(BuildGateCog(bot))
