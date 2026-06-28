import asyncio
import logging

import discord
from discord.ext import commands, tasks
from openai import AsyncOpenAI
from requests import HTTPError

from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services.bug_report_ai import BugTriage, analyze_bug_report
from bulmaai.services.bug_reports import (
    get_bug_report,
    list_tracked,
    set_status,
    set_tracked,
    upsert_triage,
)
from bulmaai.ui.bug_report_views import BugTriageView, apply_status, build_triage_embed
from bulmaai.utils.permissions import is_staff

log = logging.getLogger(__name__)


def _get_github_service(settings, repo: str) -> GitHubService:
    auth = GitHubAppAuth(
        app_id=settings.GH_APP_ID,
        installation_id=settings.GH_INSTALLATION_ID,
        private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM,
    )
    return GitHubService(
        auth=auth,
        owner=settings.GITHUB_OWNER,
        repo=repo,
        base_branch=settings.GITHUB_BASE_BRANCH,
    )


def _thread_jump_url(guild_id: int | None, thread_id: int) -> str:
    guild_part = str(guild_id) if guild_id else "@me"
    return f"https://discord.com/channels/{guild_part}/{thread_id}"


def _build_issue_body(
    triage: BugTriage,
    *,
    guild_id: int | None,
    thread_id: int,
    reporter_id: int | None,
) -> str:
    lines = [triage.summary or "_No summary provided._", ""]
    lines.append(f"**Severity:** {triage.severity.title()}")
    lines.append(f"**Affected area:** {triage.affected_area or 'Unknown'}")
    if triage.steps:
        lines.append("")
        lines.append("**Steps to reproduce:**")
        lines.extend(f"{index}. {step}" for index, step in enumerate(triage.steps, start=1))
    lines.append("")
    reporter = f"<@{reporter_id}> (`{reporter_id}`)" if reporter_id else "unknown"
    lines.append(f"_Reported via the Discord bug-report forum by {reporter}._")
    lines.append(f"_Discord thread: {_thread_jump_url(guild_id, thread_id)}_")
    return "\n".join(lines)


class BugReportsCog(commands.Cog):
    """AI-triages bug-report forum posts and tracks them to resolution."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.settings = bot.settings
        self.client = AsyncOpenAI(api_key=self.settings.openai_key)
        self._poll_lock = asyncio.Lock()
        self._poll_started = False

    # ==================== lifecycle ====================

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._start_polling_if_configured()

    def cog_unload(self) -> None:
        self.poll_tracked_issues.cancel()

    def _start_polling_if_configured(self) -> None:
        if self._poll_started or self.poll_tracked_issues.is_running():
            return
        if not getattr(self.settings, "bug_reports_enabled", False):
            log.info("Bug-report triage disabled in settings.")
            return
        if self.settings.bug_report_forum_channel_id is None:
            log.warning("Bug-report forum channel is not configured; triage stays disabled.")
            return
        self.poll_tracked_issues.change_interval(
            minutes=max(self.settings.bug_report_poll_minutes, 1),
        )
        self.poll_tracked_issues.start()
        self._poll_started = True
        log.info(
            "Bug-report tracking poll started every %s minutes.",
            self.settings.bug_report_poll_minutes,
        )

    # ==================== ingestion ====================

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if not getattr(self.settings, "bug_reports_enabled", False):
            return
        if thread.parent_id != self.settings.bug_report_forum_channel_id:
            return
        if await get_bug_report(thread.id) is not None:
            return

        starter = await self._fetch_starter_message(thread)
        if starter is not None and starter.author.bot:
            return

        report_text = starter.content if starter is not None else ""
        reporter_id = starter.author.id if starter is not None else thread.owner_id
        attachments = (
            [attachment.filename for attachment in starter.attachments]
            if starter is not None
            else []
        )
        # Forum posts carry the title separately from the body.
        full_text = f"{thread.name}\n\n{report_text}".strip()

        try:
            triage = await analyze_bug_report(
                self.client,
                model=self.settings.openai_bugreport_model,
                report_text=full_text,
                attachments=attachments,
                fallback_title=thread.name[:240] or "Bug report",
            )
        except Exception:
            log.exception("Bug-report triage failed for thread %s", thread.id)
            return

        embed = build_triage_embed(triage, status="triaged", reporter_id=reporter_id)
        try:
            message = await thread.send(embed=embed, view=BugTriageView(thread.id))
        except Exception:
            log.exception("Failed to post bug triage in thread %s", thread.id)
            return

        await upsert_triage(
            thread_id=thread.id,
            guild_id=thread.guild.id if thread.guild else None,
            reporter_id=reporter_id,
            triage_message_id=message.id,
            ai_title=triage.title,
            ai_summary=triage.summary,
        )
        log.info("Triaged bug report thread %s (is_bug=%s)", thread.id, triage.is_bug)

    async def _fetch_starter_message(self, thread: discord.Thread) -> discord.Message | None:
        if thread.starting_message is not None:
            return thread.starting_message
        try:
            return await thread.fetch_message(thread.id)
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            log.warning("Could not fetch starter message for thread %s", thread.id)
            return None

    # ==================== button interactions ====================

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("bug_issue:"):
            await self._handle_create_issue(interaction, int(custom_id.split(":")[1]))
        elif custom_id.startswith("bug_notbug:"):
            await self._handle_not_a_bug(interaction, int(custom_id.split(":")[1]))

    async def _handle_create_issue(self, interaction: discord.Interaction, thread_id: int) -> None:
        if not is_staff(interaction.user, settings=self.settings):
            return await interaction.response.send_message(
                "Only staff can create issues.", ephemeral=True
            )

        report = await get_bug_report(thread_id)
        if report is None:
            return await interaction.response.send_message(
                "No triage data found for this report.", ephemeral=True
            )
        if report.status == "tracked" and report.issue_number:
            return await interaction.response.send_message(
                f"This bug is already being tracked (issue #{report.issue_number}).",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        repo = self.settings.bug_report_repo
        service = _get_github_service(self.settings, repo)

        triage = self._triage_from_embed(interaction, report)
        body = _build_issue_body(
            triage,
            guild_id=report.guild_id or (interaction.guild_id if interaction.guild_id else None),
            thread_id=thread_id,
            reporter_id=report.reporter_id,
        )
        labels = await self._resolve_bug_labels(service)

        try:
            issue = await service.create_issue(title=triage.title, body=body, labels=labels)
        except Exception as error:
            log.exception("Failed to create issue for bug thread %s", thread_id)
            return await interaction.followup.send(
                f"Failed to create GitHub issue: {error}", ephemeral=True
            )

        await set_tracked(thread_id, repo=repo, issue_number=issue["number"])
        if interaction.message is not None and interaction.message.embeds:
            await interaction.message.edit(
                embed=apply_status(interaction.message.embeds[0], "tracked"),
                view=None,
            )
        await interaction.followup.send(
            f"Issue created and now tracked: {issue['html_url']}", ephemeral=True
        )

    async def _handle_not_a_bug(self, interaction: discord.Interaction, thread_id: int) -> None:
        if not is_staff(interaction.user, settings=self.settings):
            return await interaction.response.send_message(
                "Only staff can dismiss reports.", ephemeral=True
            )

        report = await get_bug_report(thread_id)
        if report is None:
            return await interaction.response.send_message(
                "No triage data found for this report.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        await set_status(thread_id, "dismissed")
        if interaction.message is not None and interaction.message.embeds:
            await interaction.message.edit(
                embed=apply_status(interaction.message.embeds[0], "dismissed"),
                view=None,
            )

        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            try:
                await thread.send(
                    "Thanks for the report! After review, this doesn't look like a bug we "
                    "need to act on, so we're closing this post. Feel free to open a new one "
                    "if you run into something else. 🙂"
                )
                await thread.edit(archived=True, locked=True)
            except Exception:
                log.exception("Failed to close dismissed bug thread %s", thread_id)

        await interaction.followup.send("Marked as not a bug and closed the post.", ephemeral=True)

    def _triage_from_embed(self, interaction: discord.Interaction, report) -> BugTriage:
        """Reconstruct enough triage detail from the posted embed to fill the issue."""
        title = report.ai_title or "Bug report"
        summary = report.ai_summary or ""
        severity = "medium"
        affected_area = "Unknown"
        steps: list[str] = []
        if interaction.message is not None and interaction.message.embeds:
            embed = interaction.message.embeds[0]
            for field in embed.fields:
                if field.name == "Severity":
                    severity = (field.value or "medium").strip().lower()
                elif field.name == "Affected Area":
                    affected_area = (field.value or "Unknown").strip()
                elif field.name == "Steps to Reproduce" and field.value:
                    steps = [
                        line.split(".", 1)[-1].strip()
                        for line in field.value.splitlines()
                        if line.strip()
                    ]
        return BugTriage(
            is_bug=True,
            title=title,
            summary=summary,
            severity=severity,
            affected_area=affected_area,
            steps=steps,
        )

    async def _resolve_bug_labels(self, service: GitHubService) -> list[str]:
        try:
            labels = await service.get_labels()
        except Exception:
            log.warning("Could not fetch repo labels for bug issue", exc_info=True)
            return []
        for label in labels:
            if str(label.get("name", "")).lower() == "bug":
                return [label["name"]]
        return []

    # ==================== resolution polling ====================

    @tasks.loop(minutes=10)
    async def poll_tracked_issues(self) -> None:
        async with self._poll_lock:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Bug-report tracking poll failed")

    @poll_tracked_issues.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_once(self) -> None:
        for report in await list_tracked():
            if not report.repo or report.issue_number is None:
                continue
            service = _get_github_service(self.settings, report.repo)
            try:
                issue = await service.get_issue(report.issue_number)
            except HTTPError as error:
                status_code = getattr(getattr(error, "response", None), "status_code", None)
                if status_code == 404:
                    await set_status(report.thread_id, "dismissed")
                continue
            except Exception:
                log.exception("Failed to fetch tracked issue #%s", report.issue_number)
                continue

            if issue.get("state") != "closed":
                continue
            if issue.get("state_reason") == "not_planned":
                await set_status(report.thread_id, "dismissed")
                continue

            await self._resolve_report(report)

    async def _resolve_report(self, report) -> None:
        thread = self.bot.get_channel(report.thread_id)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(report.thread_id)
            except Exception:
                log.warning("Could not fetch thread %s to resolve bug report", report.thread_id)
                await set_status(report.thread_id, "resolved")
                return

        if not isinstance(thread, discord.Thread):
            await set_status(report.thread_id, "resolved")
            return

        mention = f"<@{report.reporter_id}> " if report.reporter_id else ""
        try:
            await thread.send(
                f"{mention}Good news! 🎉 The issue you reported has been **fixed** and will be "
                "included in an upcoming update. Thanks a lot for taking the time to report it!",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            if report.triage_message_id:
                try:
                    triage_message = await thread.fetch_message(report.triage_message_id)
                    if triage_message.embeds:
                        await triage_message.edit(
                            embed=apply_status(triage_message.embeds[0], "resolved"),
                            view=None,
                        )
                except Exception:
                    log.warning("Could not update triage embed for thread %s", report.thread_id)
            await thread.edit(archived=True, locked=True)
        except Exception:
            log.exception("Failed to post resolution for thread %s", report.thread_id)
            return

        await set_status(report.thread_id, "resolved")
        log.info(
            "Resolved bug report thread %s from closed issue #%s",
            report.thread_id,
            report.issue_number,
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(BugReportsCog(bot))
