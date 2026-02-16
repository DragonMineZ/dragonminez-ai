import logging
import discord
from discord.ext import commands

from bulmaai.config import load_settings
from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_issues import GitHubIssuesService
from bulmaai.ui.github_views import (
    CreateIssueModal,
    AddCommentModal,
    CloseReasonModal,
    LabelSelectView,
    IssueManagementView,
    QuickIssueSelect,
)
from bulmaai.utils.permissions import is_staff

log = logging.getLogger(__name__)
settings = load_settings()


def _get_issues_service(owner: str | None = None, repo: str | None = None) -> GitHubIssuesService:
    auth = GitHubAppAuth(
        app_id=settings.GH_APP_ID,
        installation_id=settings.GH_INSTALLATION_ID,
        private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM,
    )
    return GitHubIssuesService(
        auth=auth,
        owner=owner or settings.GITHUB_OWNER,
        repo=repo or settings.GITHUB_REPO,
    )


def _build_issue_embed(issue: dict, owner: str, repo: str) -> discord.Embed:
    state_emoji = "🟢" if issue["state"] == "open" else "🔴"
    embed = discord.Embed(
        title=f"{state_emoji} #{issue['number']}: {issue['title']}",
        url=issue["html_url"],
        color=discord.Color.green() if issue["state"] == "open" else discord.Color.red(),
    )
    body = issue.get("body") or "No description"
    embed.description = body[:500] + "..." if len(body) > 500 else body

    labels = issue.get("labels", [])
    if labels:
        label_str = " ".join(f"`{l['name']}`" for l in labels[:10])
        embed.add_field(name="Labels", value=label_str, inline=False)

    assignees = issue.get("assignees", [])
    if assignees:
        assignee_str = ", ".join(a["login"] for a in assignees[:5])
        embed.add_field(name="Assignees", value=assignee_str, inline=True)

    embed.add_field(name="State", value=issue["state"].title(), inline=True)
    embed.set_footer(text=f"{owner}/{repo}")
    return embed


class GitHubCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.owner = settings.GITHUB_OWNER
        self.repo = settings.GITHUB_REPO

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(PersistentGitHubView(self))
        log.info("Persistent GitHub views registered")

    github = discord.SlashCommandGroup("github", "GitHub issue management commands")

    @github.command(name="create", description="Create a new GitHub issue with labels")
    @discord.option("repo", description="Repository name (optional)", required=False)
    async def create_issue(self, ctx: discord.ApplicationContext, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can create issues.", ephemeral=True)

        await ctx.defer(ephemeral=True)

        target_repo = repo or self.repo
        service = _get_issues_service(self.owner, target_repo)

        try:
            labels = await service.get_labels()
        except Exception as e:
            log.exception("Failed to fetch labels")
            return await ctx.followup.send(f"Failed to fetch labels: {e}", ephemeral=True)

        label_view = LabelSelectView(labels)
        await ctx.followup.send(
            "**Step 1/2:** Select labels for the new issue (or skip):",
            view=label_view,
            ephemeral=True,
        )

        await label_view.wait()
        if not label_view.confirmed:
            return await ctx.followup.send("Issue creation cancelled.", ephemeral=True)

        modal = CreateIssueModal(selected_labels=label_view.selected_labels)
        dummy_interaction = await self._create_modal_interaction(ctx, modal)
        if not dummy_interaction or not modal.result:
            return

        try:
            issue = await service.create_issue(
                title=modal.result["title"],
                body=modal.result["body"],
                labels=modal.result["labels"],
            )
        except Exception as e:
            log.exception("Failed to create issue")
            return await ctx.followup.send(f"Failed to create issue: {e}", ephemeral=True)

        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = IssueManagementView(
            issue_number=issue["number"],
            owner=self.owner,
            repo=target_repo,
            issue_state="open",
        )
        await ctx.followup.send(f"✅ Issue created successfully!", embed=embed, view=view)

    async def _create_modal_interaction(self, ctx: discord.ApplicationContext, modal: CreateIssueModal):
        btn_view = discord.ui.View(timeout=60)
        open_modal_btn = discord.ui.Button(label="Enter Issue Details", style=discord.ButtonStyle.primary)

        async def open_modal(interaction: discord.Interaction):
            await interaction.response.send_modal(modal)

        open_modal_btn.callback = open_modal
        btn_view.add_item(open_modal_btn)

        msg = await ctx.followup.send("**Step 2/2:** Click to enter issue details:", view=btn_view, ephemeral=True)
        await modal.wait()
        try:
            await msg.delete()
        except:
            pass
        return modal.result

    @github.command(name="close", description="Close a GitHub issue")
    @discord.option("issue_number", description="Issue number to close", required=True)
    @discord.option("repo", description="Repository name (optional)", required=False)
    async def close_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can close issues.", ephemeral=True)

        target_repo = repo or self.repo
        service = _get_issues_service(self.owner, target_repo)

        modal = CloseReasonModal(issue_number)
        await ctx.send_modal(modal)
        await modal.wait()

        if not modal.reason:
            return

        await ctx.respond("Closing issue...", ephemeral=True)

        try:
            if modal.comment:
                await service.add_comment(issue_number, modal.comment)
            issue = await service.close_issue(issue_number, reason=modal.reason)
        except Exception as e:
            log.exception("Failed to close issue")
            return await ctx.followup.send(f"Failed to close issue: {e}", ephemeral=True)

        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = IssueManagementView(
            issue_number=issue_number,
            owner=self.owner,
            repo=target_repo,
            issue_state="closed",
        )
        await ctx.followup.send(f"🔒 Issue #{issue_number} closed!", embed=embed, view=view)

    @github.command(name="reopen", description="Reopen a closed GitHub issue")
    @discord.option("issue_number", description="Issue number to reopen", required=True)
    @discord.option("repo", description="Repository name (optional)", required=False)
    async def reopen_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can reopen issues.", ephemeral=True)

        await ctx.defer(ephemeral=True)
        target_repo = repo or self.repo
        service = _get_issues_service(self.owner, target_repo)

        try:
            issue = await service.reopen_issue(issue_number)
        except Exception as e:
            log.exception("Failed to reopen issue")
            return await ctx.followup.send(f"Failed to reopen issue: {e}", ephemeral=True)

        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = IssueManagementView(
            issue_number=issue_number,
            owner=self.owner,
            repo=target_repo,
            issue_state="open",
        )
        await ctx.followup.send(f"🔓 Issue #{issue_number} reopened!", embed=embed, view=view)

    @github.command(name="view", description="View a GitHub issue")
    @discord.option("issue_number", description="Issue number to view", required=True)
    @discord.option("repo", description="Repository name (optional)", required=False)
    async def view_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        await ctx.defer(ephemeral=True)
        target_repo = repo or self.repo
        service = _get_issues_service(self.owner, target_repo)

        try:
            issue = await service.get_issue(issue_number)
        except Exception as e:
            log.exception("Failed to fetch issue")
            return await ctx.followup.send(f"Failed to fetch issue: {e}", ephemeral=True)

        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = IssueManagementView(
            issue_number=issue_number,
            owner=self.owner,
            repo=target_repo,
            issue_state=issue["state"],
        )
        await ctx.followup.send(embed=embed, view=view)

    @github.command(name="list", description="List open GitHub issues")
    @discord.option("state", description="Issue state", choices=["open", "closed", "all"], required=False)
    @discord.option("label", description="Filter by label", required=False)
    @discord.option("repo", description="Repository name (optional)", required=False)
    async def list_issues(self, ctx: discord.ApplicationContext, state: str = "open", label: str = None, repo: str = None):
        await ctx.defer(ephemeral=True)
        target_repo = repo or self.repo
        service = _get_issues_service(self.owner, target_repo)

        try:
            issues = await service.list_issues(state=state, labels=label)
        except Exception as e:
            log.exception("Failed to list issues")
            return await ctx.followup.send(f"Failed to list issues: {e}", ephemeral=True)

        issues = [i for i in issues if "pull_request" not in i]

        if not issues:
            return await ctx.followup.send(f"No {state} issues found.", ephemeral=True)

        embed = discord.Embed(
            title=f"📋 {state.title()} Issues - {self.owner}/{target_repo}",
            color=discord.Color.blurple(),
        )

        desc_lines = []
        for issue in issues[:15]:
            state_emoji = "🟢" if issue["state"] == "open" else "🔴"
            labels = " ".join(f"`{l['name']}`" for l in issue.get("labels", [])[:3])
            desc_lines.append(f"{state_emoji} **#{issue['number']}** [{issue['title'][:50]}]({issue['html_url']}) {labels}")

        embed.description = "\n".join(desc_lines)
        if len(issues) > 15:
            embed.set_footer(text=f"Showing 15 of {len(issues)} issues")

        select_view = QuickIssueSelect(issues, owner=self.owner, repo=target_repo)
        msg = await ctx.followup.send("Select an issue to view details:", embed=embed, view=select_view, ephemeral=True)

        await select_view.wait()
        if select_view.selected_issue:
            issue = await service.get_issue(select_view.selected_issue["number"])
            detail_embed = _build_issue_embed(issue, self.owner, target_repo)
            view = IssueManagementView(
                issue_number=issue["number"],
                owner=self.owner,
                repo=target_repo,
                issue_state=issue["state"],
            )
            await ctx.followup.send(embed=detail_embed, view=view)

    @github.command(name="comment", description="Add a comment to a GitHub issue")
    @discord.option("issue_number", description="Issue number", required=True)
    @discord.option("repo", description="Repository name (optional)", required=False)
    async def add_comment(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can comment on issues.", ephemeral=True)

        target_repo = repo or self.repo
        service = _get_issues_service(self.owner, target_repo)

        modal = AddCommentModal(issue_number)
        await ctx.send_modal(modal)
        await modal.wait()

        if not modal.comment:
            return

        await ctx.respond("Adding comment...", ephemeral=True)

        try:
            await service.add_comment(issue_number, modal.comment)
            issue = await service.get_issue(issue_number)
        except Exception as e:
            log.exception("Failed to add comment")
            return await ctx.followup.send(f"Failed to add comment: {e}", ephemeral=True)

        embed = _build_issue_embed(issue, self.owner, target_repo)
        await ctx.followup.send(f"💬 Comment added to issue #{issue_number}!", embed=embed)

    @github.command(name="labels", description="View available labels for a repository")
    @discord.option("repo", description="Repository name (optional)", required=False)
    async def list_labels(self, ctx: discord.ApplicationContext, repo: str = None):
        await ctx.defer(ephemeral=True)
        target_repo = repo or self.repo
        service = _get_issues_service(self.owner, target_repo)

        try:
            labels = await service.get_labels()
        except Exception as e:
            log.exception("Failed to fetch labels")
            return await ctx.followup.send(f"Failed to fetch labels: {e}", ephemeral=True)

        if not labels:
            return await ctx.followup.send("No labels found.", ephemeral=True)

        embed = discord.Embed(
            title=f"🏷️ Labels - {self.owner}/{target_repo}",
            color=discord.Color.blurple(),
        )

        label_lines = []
        for label in labels[:25]:
            color_hex = f"#{label['color']}" if label.get("color") else ""
            desc = f" - {label['description'][:50]}" if label.get("description") else ""
            label_lines.append(f"• **{label['name']}** {color_hex}{desc}")

        embed.description = "\n".join(label_lines)
        await ctx.followup.send(embed=embed)

    @github.command(name="addlabel", description="Add a label to an issue")
    @discord.option("issue_number", description="Issue number", required=True)
    @discord.option("repo", description="Repository name (optional)", required=False)
    async def add_label_to_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can add labels.", ephemeral=True)

        await ctx.defer(ephemeral=True)
        target_repo = repo or self.repo
        service = _get_issues_service(self.owner, target_repo)

        try:
            labels = await service.get_labels()
        except Exception as e:
            return await ctx.followup.send(f"Failed to fetch labels: {e}", ephemeral=True)

        label_view = LabelSelectView(labels)
        await ctx.followup.send(f"Select labels to add to issue #{issue_number}:", view=label_view, ephemeral=True)

        await label_view.wait()
        if not label_view.confirmed or not label_view.selected_labels:
            return await ctx.followup.send("No labels selected.", ephemeral=True)

        try:
            await service.add_labels(issue_number, label_view.selected_labels)
            issue = await service.get_issue(issue_number)
        except Exception as e:
            return await ctx.followup.send(f"Failed to add labels: {e}", ephemeral=True)

        embed = _build_issue_embed(issue, self.owner, target_repo)
        await ctx.followup.send(f"🏷️ Labels added to issue #{issue_number}!", embed=embed)


class PersistentGitHubView(discord.ui.View):
    def __init__(self, cog: GitHubCog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Close Issue", style=discord.ButtonStyle.danger, custom_id="gh_close_persistent")
    async def close_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        pass

    @discord.ui.button(label="Reopen Issue", style=discord.ButtonStyle.success, custom_id="gh_reopen_persistent")
    async def reopen_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        pass

    @discord.ui.button(label="Add Comment", style=discord.ButtonStyle.primary, custom_id="gh_comment_persistent")
    async def comment_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        pass


class GitHubCogWithListeners(GitHubCog):
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "")

        if custom_id.startswith("gh_close:"):
            await self._handle_close(interaction, custom_id)
        elif custom_id.startswith("gh_reopen:"):
            await self._handle_reopen(interaction, custom_id)
        elif custom_id.startswith("gh_comment:"):
            await self._handle_comment(interaction, custom_id)

    async def _handle_close(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can close issues.", ephemeral=True)

        parts = custom_id.split(":")
        if len(parts) < 4:
            return await interaction.response.send_message("Invalid button data.", ephemeral=True)

        owner, repo, issue_number = parts[1], parts[2], int(parts[3])
        service = _get_issues_service(owner, repo)

        modal = CloseReasonModal(issue_number)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if not modal.reason:
            return

        try:
            if modal.comment:
                await service.add_comment(issue_number, modal.comment)
            issue = await service.close_issue(issue_number, reason=modal.reason)
        except Exception as e:
            return await interaction.followup.send(f"Failed to close issue: {e}", ephemeral=True)

        embed = _build_issue_embed(issue, owner, repo)
        view = IssueManagementView(issue_number=issue_number, owner=owner, repo=repo, issue_state="closed")

        await interaction.message.edit(embed=embed, view=view)
        await interaction.followup.send(f"🔒 Issue #{issue_number} closed!", ephemeral=True)

    async def _handle_reopen(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can reopen issues.", ephemeral=True)

        parts = custom_id.split(":")
        if len(parts) < 4:
            return await interaction.response.send_message("Invalid button data.", ephemeral=True)

        owner, repo, issue_number = parts[1], parts[2], int(parts[3])
        service = _get_issues_service(owner, repo)

        await interaction.response.defer(ephemeral=True)

        try:
            issue = await service.reopen_issue(issue_number)
        except Exception as e:
            return await interaction.followup.send(f"Failed to reopen issue: {e}", ephemeral=True)

        embed = _build_issue_embed(issue, owner, repo)
        view = IssueManagementView(issue_number=issue_number, owner=owner, repo=repo, issue_state="open")

        await interaction.message.edit(embed=embed, view=view)
        await interaction.followup.send(f"🔓 Issue #{issue_number} reopened!", ephemeral=True)

    async def _handle_comment(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can comment.", ephemeral=True)

        parts = custom_id.split(":")
        if len(parts) < 4:
            return await interaction.response.send_message("Invalid button data.", ephemeral=True)

        owner, repo, issue_number = parts[1], parts[2], int(parts[3])
        service = _get_issues_service(owner, repo)

        modal = AddCommentModal(issue_number)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if not modal.comment:
            return

        try:
            await service.add_comment(issue_number, modal.comment)
        except Exception as e:
            return await interaction.followup.send(f"Failed to add comment: {e}", ephemeral=True)

        await interaction.followup.send(f"💬 Comment added to issue #{issue_number}!", ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(GitHubCogWithListeners(bot))
