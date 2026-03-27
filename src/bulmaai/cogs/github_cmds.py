import logging

import discord
from discord.ext import commands

from bulmaai.config import load_settings
from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.ui.github_views import (
    AddCommentModal,
    CloseReasonModal,
    CreateIssueModal,
    IssueBoardView,
    LabelSelectView,
    MergeConfirmView,
    PRBoardView,
    PRCommentModal,
)
from bulmaai.utils.permissions import is_staff

log = logging.getLogger(__name__)
settings = load_settings()


async def repo_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    current = (ctx.value or "").lower()
    return [repo for repo in settings.GITHUB_REPOS if current in repo.lower()][:25]


def _get_github_service(repo: str | None = None) -> GitHubService:
    auth = GitHubAppAuth(
        app_id=settings.GH_APP_ID,
        installation_id=settings.GH_INSTALLATION_ID,
        private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM,
    )
    target_repo = repo or settings.GITHUB_DEFAULT_REPO
    whitelist_path = settings.GITHUB_WHITELIST_FILE_PATH if target_repo == settings.GITHUB_WHITELIST_REPO else None
    return GitHubService(
        auth=auth,
        owner=settings.GITHUB_OWNER,
        repo=target_repo,
        base_branch=settings.GITHUB_BASE_BRANCH,
        whitelist_file_path=whitelist_path,
    )


def _build_issue_embed(issue: dict, owner: str, repo: str) -> discord.Embed:
    state_emoji = "🟢" if issue["state"] == "open" else "🔴"
    embed = discord.Embed(
        title=f"{state_emoji} #{issue['number']}: {issue['title']}",
        url=issue["html_url"],
        color=discord.Color.green() if issue["state"] == "open" else discord.Color.red(),
    )
    body = issue.get("body") or "No description"
    embed.description = body[:1500] + "..." if len(body) > 1500 else body

    labels = issue.get("labels", [])
    if labels:
        embed.add_field(name="Labels", value=" ".join(f"`{label['name']}`" for label in labels[:10]), inline=False)

    assignees = issue.get("assignees", [])
    if assignees:
        embed.add_field(name="Assignees", value=", ".join(assignee["login"] for assignee in assignees[:5]), inline=True)

    embed.add_field(name="State", value=issue["state"].title(), inline=True)
    embed.set_footer(text=f"{owner}/{repo}")
    return embed


def _build_issue_list_embed(issues: list[dict], owner: str, repo: str, state: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"📋 {state.title()} Issues - {owner}/{repo}",
        color=discord.Color.blurple(),
    )
    lines = []
    for issue in issues[:15]:
        state_emoji = "🟢" if issue["state"] == "open" else "🔴"
        labels = " ".join(f"`{label['name']}`" for label in issue.get("labels", [])[:3])
        lines.append(f"{state_emoji} **#{issue['number']}** [{issue['title'][:50]}]({issue['html_url']}) {labels}")
    embed.description = "\n".join(lines) or "No issues found."
    if len(issues) > 15:
        embed.set_footer(text=f"Showing 15 of {len(issues)} issues")
    return embed


def _build_pr_embed(pr: dict, owner: str, repo: str) -> discord.Embed:
    merged = pr.get("merged", False)
    draft = pr.get("draft", False)

    if merged:
        state_emoji, color, state_text = "🟣", discord.Color.purple(), "Merged"
    elif pr["state"] == "open":
        state_emoji = "📝" if draft else "🟢"
        color = discord.Color.dark_grey() if draft else discord.Color.green()
        state_text = "Draft" if draft else "Open"
    else:
        state_emoji, color, state_text = "🔴", discord.Color.red(), "Closed"

    embed = discord.Embed(
        title=f"{state_emoji} PR #{pr['number']}: {pr['title']}",
        url=pr["html_url"],
        color=color,
    )
    body = pr.get("body") or "No description"
    embed.description = body[:1500] + "..." if len(body) > 1500 else body
    embed.add_field(name="State", value=state_text, inline=True)
    embed.add_field(name="Branch", value=f"`{pr['head']['ref']}` -> `{pr['base']['ref']}`", inline=True)
    if pr.get("user"):
        embed.add_field(name="Author", value=pr["user"]["login"], inline=True)
    labels = pr.get("labels", [])
    if labels:
        embed.add_field(name="Labels", value=" ".join(f"`{label['name']}`" for label in labels[:10]), inline=False)
    reviewers = pr.get("requested_reviewers", [])
    if reviewers:
        embed.add_field(name="Reviewers", value=", ".join(reviewer["login"] for reviewer in reviewers[:5]), inline=True)
    stats = []
    if pr.get("additions") is not None:
        stats.append(f"**+{pr['additions']}** / **-{pr['deletions']}**")
    if pr.get("changed_files") is not None:
        stats.append(f"{pr['changed_files']} file(s)")
    if stats:
        embed.add_field(name="Changes", value=" · ".join(stats), inline=True)
    if pr.get("mergeable_state"):
        embed.add_field(name="Mergeable", value=pr["mergeable_state"].replace("_", " ").title(), inline=True)
    embed.set_footer(text=f"{owner}/{repo}")
    return embed


def _build_pr_list_embed(prs: list[dict], owner: str, repo: str, state: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"📋 {state.title()} Pull Requests - {owner}/{repo}",
        color=discord.Color.blurple(),
    )
    lines = []
    for pr in prs[:15]:
        merged = pr.get("merged_at") is not None
        draft = pr.get("draft", False)
        if merged:
            emoji = "🟣"
        elif pr["state"] == "open":
            emoji = "📝" if draft else "🟢"
        else:
            emoji = "🔴"
        lines.append(f"{emoji} **#{pr['number']}** [{pr['title'][:50]}]({pr['html_url']}) by `{pr['user']['login']}`")
    embed.description = "\n".join(lines) or "No pull requests found."
    if len(prs) > 15:
        embed.set_footer(text=f"Showing 15 of {len(prs)} pull requests")
    return embed


class GitHubCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.owner = settings.GITHUB_OWNER
        self.default_repo = settings.GITHUB_DEFAULT_REPO

    github = discord.SlashCommandGroup("github", "GitHub issue management commands")

    async def _load_issue_board_view(
        self,
        *,
        repo: str,
        issue_number: int,
        issue_state: str,
    ) -> IssueBoardView:
        service = _get_github_service(repo)
        issues = await service.list_issues(state="all")
        issues = [issue for issue in issues if "pull_request" not in issue]
        return IssueBoardView(
            issues=issues or [{"number": issue_number, "title": f"Issue #{issue_number}", "labels": []}],
            owner=self.owner,
            repo=repo,
            issue_number=issue_number,
            issue_state=issue_state,
        )

    async def _load_pr_board_view(
        self,
        *,
        repo: str,
        pr_number: int,
        pr_state: str,
        merged: bool,
    ) -> PRBoardView:
        service = _get_github_service(repo)
        prs = await service.list_prs(state="all")
        return PRBoardView(
            prs=prs or [{"number": pr_number, "title": f"PR #{pr_number}", "user": {"login": "unknown"}}],
            owner=self.owner,
            repo=repo,
            pr_number=pr_number,
            pr_state=pr_state,
            merged=merged,
        )

    async def _prompt_issue_modal(self, ctx: discord.ApplicationContext, modal: CreateIssueModal) -> dict | None:
        prompt_view = discord.ui.View(timeout=300)
        open_modal_button = discord.ui.Button(label="Enter Issue Details", style=discord.ButtonStyle.primary)

        async def open_modal(interaction: discord.Interaction):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Only the command author can submit this modal.")
            await interaction.response.send_modal(modal)

        open_modal_button.callback = open_modal
        prompt_view.add_item(open_modal_button)

        prompt_message = await ctx.followup.send(
            "**Step 2/2:** Click to enter the issue details.",
            view=prompt_view,
        )
        await modal.wait()
        await prompt_message.edit(content="Issue details submitted.", view=None)
        return modal.result

    @github.command(name="create", description="Create a new GitHub issue with labels")
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def create_issue(self, ctx: discord.ApplicationContext, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can create issues.")

        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            labels = await service.get_labels()
        except Exception as error:
            log.exception("Failed to fetch labels")
            return await ctx.followup.send(f"Failed to fetch labels: {error}")

        label_view = LabelSelectView(labels)
        selection_message = await ctx.followup.send(
            "**Step 1/2:** Select labels for the new issue or skip them.",
            view=label_view,
        )
        await label_view.wait()
        if not label_view.confirmed:
            await selection_message.edit(content="Issue creation cancelled.", view=None)
            return

        modal = CreateIssueModal(selected_labels=label_view.selected_labels)
        result = await self._prompt_issue_modal(ctx, modal)
        if not result:
            return

        try:
            issue = await service.create_issue(
                title=result["title"],
                body=result["body"],
                labels=result["labels"],
            )
        except Exception as error:
            log.exception("Failed to create issue")
            return await ctx.followup.send(f"Failed to create issue: {error}")

        await selection_message.edit(content="Issue created.", view=None)
        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue["number"],
            issue_state="open",
        )
        await ctx.followup.send("Issue created successfully.", embed=embed, view=view)

    @github.command(name="close", description="Close a GitHub issue")
    @discord.option("issue_number", description="Issue number to close", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def close_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can close issues.")

        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)
        modal = CloseReasonModal(issue_number)
        await ctx.send_modal(modal)
        await modal.wait()
        if not modal.reason:
            return

        try:
            if modal.comment:
                await service.add_issue_comment(issue_number, modal.comment)
            issue = await service.close_issue(issue_number, reason=modal.reason)
        except Exception as error:
            log.exception("Failed to close issue")
            return await ctx.followup.send(f"Failed to close issue: {error}")

        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state="closed",
        )
        await ctx.followup.send(f"Issue #{issue_number} closed by {ctx.author.mention}.", embed=embed, view=view)

    @github.command(name="reopen", description="Reopen a closed GitHub issue")
    @discord.option("issue_number", description="Issue number to reopen", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def reopen_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can reopen issues.")

        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            issue = await service.reopen_issue(issue_number)
        except Exception as error:
            log.exception("Failed to reopen issue")
            return await ctx.followup.send(f"Failed to reopen issue: {error}")

        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state="open",
        )
        await ctx.followup.send(f"Issue #{issue_number} reopened by {ctx.author.mention}.", embed=embed, view=view)

    @github.command(name="view", description="View a GitHub issue")
    @discord.option("issue_number", description="Issue number to view", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def view_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            issue = await service.get_issue(issue_number)
        except Exception as error:
            log.exception("Failed to fetch issue")
            return await ctx.followup.send(f"Failed to fetch issue: {error}")

        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await ctx.followup.send(embed=embed, view=view)

    @github.command(name="list", description="List GitHub issues")
    @discord.option("state", description="Issue state", choices=["open", "closed", "all"], required=False)
    @discord.option("label", description="Filter by label", required=False)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def list_issues(self, ctx: discord.ApplicationContext, state: str = "open", label: str = None, repo: str = None):
        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            issues = await service.list_issues(state=state, labels=label)
        except Exception as error:
            log.exception("Failed to list issues")
            return await ctx.followup.send(f"Failed to list issues: {error}")

        issues = [issue for issue in issues if "pull_request" not in issue]
        if not issues:
            return await ctx.followup.send(f"No {state} issues found.")

        embed = _build_issue_list_embed(issues, self.owner, target_repo, state)
        view = IssueBoardView(issues=issues, owner=self.owner, repo=target_repo)
        await ctx.followup.send(
            "Issue board. Use the dropdown to open an issue and keep working on the same message.",
            embed=embed,
            view=view,
        )

    @github.command(name="comment", description="Add a comment to a GitHub issue")
    @discord.option("issue_number", description="Issue number", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def add_comment(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can comment on issues.")

        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)
        modal = AddCommentModal(issue_number)
        await ctx.send_modal(modal)
        await modal.wait()
        if not modal.comment:
            return

        try:
            await service.add_issue_comment(issue_number, modal.comment)
            issue = await service.get_issue(issue_number)
        except Exception as error:
            log.exception("Failed to add comment")
            return await ctx.followup.send(f"Failed to add comment: {error}")

        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await ctx.followup.send(f"Comment added to issue #{issue_number} by {ctx.author.mention}.", embed=embed, view=view)

    @github.command(name="labels", description="View available labels for a repository")
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def list_labels(self, ctx: discord.ApplicationContext, repo: str = None):
        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            labels = await service.get_labels()
        except Exception as error:
            log.exception("Failed to fetch labels")
            return await ctx.followup.send(f"Failed to fetch labels: {error}")

        if not labels:
            return await ctx.followup.send("No labels found.")

        embed = discord.Embed(
            title=f"🏷️ Labels - {self.owner}/{target_repo}",
            color=discord.Color.blurple(),
        )
        embed.description = "\n".join(
            f"• **{label['name']}** #{label['color']}" +
            (f" - {label['description'][:50]}" if label.get("description") else "")
            for label in labels[:25]
        )
        await ctx.followup.send(embed=embed)

    @github.command(name="addlabel", description="Add a label to an issue")
    @discord.option("issue_number", description="Issue number", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def add_label_to_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can add labels.")

        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            labels = await service.get_labels()
        except Exception as error:
            return await ctx.followup.send(f"Failed to fetch labels: {error}")

        label_view = LabelSelectView(labels)
        prompt_message = await ctx.followup.send(
            f"Select labels to add to issue #{issue_number}:",
            view=label_view,
        )
        await label_view.wait()
        if not label_view.confirmed or not label_view.selected_labels:
            await prompt_message.edit(content="No labels selected.", view=None)
            return

        try:
            await service.add_labels(issue_number, label_view.selected_labels)
            issue = await service.get_issue(issue_number)
        except Exception as error:
            return await ctx.followup.send(f"Failed to add labels: {error}")

        await prompt_message.edit(content="Labels updated.", view=None)
        embed = _build_issue_embed(issue, self.owner, target_repo)
        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await ctx.followup.send(f"Labels added to issue #{issue_number}.", embed=embed, view=view)

    pr = github.create_subgroup("pr", "GitHub pull request management")

    @pr.command(name="list", description="List pull requests")
    @discord.option("state", description="PR state", choices=["open", "closed", "all"], required=False)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def list_prs(self, ctx: discord.ApplicationContext, state: str = "open", repo: str = None):
        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            prs = await service.list_prs(state=state)
        except Exception as error:
            log.exception("Failed to list PRs")
            return await ctx.followup.send(f"Failed to list pull requests: {error}")

        if not prs:
            return await ctx.followup.send(f"No {state} pull requests found.")

        embed = _build_pr_list_embed(prs, self.owner, target_repo, state)
        view = PRBoardView(prs=prs, owner=self.owner, repo=target_repo)
        await ctx.followup.send(
            "Pull request board. Use the dropdown to switch PRs on this message.",
            embed=embed,
            view=view,
        )

    @pr.command(name="view", description="View a pull request")
    @discord.option("pr_number", description="PR number to view", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def view_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            pr = await service.get_pr(pr_number)
        except Exception as error:
            log.exception("Failed to fetch PR")
            return await ctx.followup.send(f"Failed to fetch pull request: {error}")

        embed = _build_pr_embed(pr, self.owner, target_repo)
        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=pr.get("merged", False),
        )
        await ctx.followup.send(embed=embed, view=view)

    @pr.command(name="merge", description="Merge a pull request")
    @discord.option("pr_number", description="PR number to merge", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def merge_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can merge pull requests.")

        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        confirm_view = MergeConfirmView()
        prompt_message = await ctx.followup.send(
            f"Merge PR #{pr_number}. Select a merge method and confirm:",
            view=confirm_view,
        )
        await confirm_view.wait()
        if not confirm_view.confirmed or not confirm_view.merge_method:
            await prompt_message.edit(content="Merge cancelled.", view=None)
            return

        try:
            await service.merge_pr(pr_number, merge_method=confirm_view.merge_method)
            pr = await service.get_pr(pr_number)
        except Exception as error:
            log.exception("Failed to merge PR")
            return await ctx.followup.send(f"Failed to merge pull request: {error}")

        await prompt_message.edit(content="Merge completed.", view=None)
        embed = _build_pr_embed(pr, self.owner, target_repo)
        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=True,
        )
        await ctx.followup.send(
            f"PR #{pr_number} merged via **{confirm_view.merge_method}** by {ctx.author.mention}.",
            embed=embed,
            view=view,
        )

    @pr.command(name="close", description="Close a pull request")
    @discord.option("pr_number", description="PR number to close", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def close_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can close pull requests.")

        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            pr = await service.close_pr(pr_number)
        except Exception as error:
            log.exception("Failed to close PR")
            return await ctx.followup.send(f"Failed to close pull request: {error}")

        embed = _build_pr_embed(pr, self.owner, target_repo)
        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state="closed",
            merged=False,
        )
        await ctx.followup.send(f"PR #{pr_number} closed by {ctx.author.mention}.", embed=embed, view=view)

    @pr.command(name="reopen", description="Reopen a closed pull request")
    @discord.option("pr_number", description="PR number to reopen", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def reopen_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can reopen pull requests.")

        await ctx.defer()
        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)

        try:
            pr = await service.reopen_pr(pr_number)
        except Exception as error:
            log.exception("Failed to reopen PR")
            return await ctx.followup.send(f"Failed to reopen pull request: {error}")

        embed = _build_pr_embed(pr, self.owner, target_repo)
        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state="open",
            merged=False,
        )
        await ctx.followup.send(f"PR #{pr_number} reopened by {ctx.author.mention}.", embed=embed, view=view)

    @pr.command(name="comment", description="Add a comment to a pull request")
    @discord.option("pr_number", description="PR number", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def comment_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can comment on pull requests.")

        target_repo = repo or self.default_repo
        service = _get_github_service(target_repo)
        modal = PRCommentModal(pr_number)
        await ctx.send_modal(modal)
        await modal.wait()
        if not modal.comment:
            return

        try:
            await service.add_pr_comment(pr_number, modal.comment)
            pr = await service.get_pr(pr_number)
        except Exception as error:
            log.exception("Failed to add PR comment")
            return await ctx.followup.send(f"Failed to add comment: {error}")

        embed = _build_pr_embed(pr, self.owner, target_repo)
        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=pr.get("merged", False),
        )
        await ctx.followup.send(f"Comment added to PR #{pr_number} by {ctx.author.mention}.", embed=embed, view=view)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("gh_issue_select:"):
            await self._handle_issue_select(interaction, custom_id)
        elif custom_id.startswith("gh_pr_select:"):
            await self._handle_pr_select(interaction, custom_id)
        elif custom_id.startswith("gh_close:"):
            await self._handle_close(interaction, custom_id)
        elif custom_id.startswith("gh_reopen:"):
            await self._handle_reopen(interaction, custom_id)
        elif custom_id.startswith("gh_comment:"):
            await self._handle_comment(interaction, custom_id)
        elif custom_id.startswith("gh_pr_merge:"):
            await self._handle_pr_merge(interaction, custom_id)
        elif custom_id.startswith("gh_pr_close:"):
            await self._handle_pr_close(interaction, custom_id)
        elif custom_id.startswith("gh_pr_reopen:"):
            await self._handle_pr_reopen(interaction, custom_id)
        elif custom_id.startswith("gh_pr_comment:"):
            await self._handle_pr_comment(interaction, custom_id)

    async def _handle_issue_select(self, interaction: discord.Interaction, custom_id: str):
        parts = custom_id.split(":")
        owner, repo = parts[1], parts[2]
        issue_number = int(interaction.data.get("values", [None])[0])
        service = _get_github_service(repo)

        issue = await service.get_issue(issue_number)
        issues = [item for item in await service.list_issues(state="all") if "pull_request" not in item]
        embed = _build_issue_embed(issue, owner, repo)
        view = IssueBoardView(
            issues=issues,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await interaction.response.edit_message(
            content=f"Issue board for {owner}/{repo}",
            embed=embed,
            view=view,
        )

    async def _handle_pr_select(self, interaction: discord.Interaction, custom_id: str):
        parts = custom_id.split(":")
        owner, repo = parts[1], parts[2]
        pr_number = int(interaction.data.get("values", [None])[0])
        service = _get_github_service(repo)

        pr = await service.get_pr(pr_number)
        prs = await service.list_prs(state="all")
        embed = _build_pr_embed(pr, owner, repo)
        view = PRBoardView(
            prs=prs,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=pr.get("merged", False),
        )
        await interaction.response.edit_message(
            content=f"Pull request board for {owner}/{repo}",
            embed=embed,
            view=view,
        )

    async def _handle_close(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can close issues.")

        _, owner, repo, issue_number_raw = custom_id.split(":")
        issue_number = int(issue_number_raw)
        service = _get_github_service(repo)

        modal = CloseReasonModal(issue_number)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not modal.reason:
            return

        try:
            if modal.comment:
                await service.add_issue_comment(issue_number, modal.comment)
            issue = await service.close_issue(issue_number, reason=modal.reason)
        except Exception as error:
            return await interaction.followup.send(f"Failed to close issue: {error}")

        view = await self._load_issue_board_view(
            repo=repo,
            issue_number=issue_number,
            issue_state="closed",
        )
        await interaction.message.edit(embed=_build_issue_embed(issue, owner, repo), view=view)
        await interaction.followup.send(f"Issue #{issue_number} closed by {interaction.user.mention}.")

    async def _handle_reopen(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can reopen issues.")

        _, owner, repo, issue_number_raw = custom_id.split(":")
        issue_number = int(issue_number_raw)
        service = _get_github_service(repo)
        await interaction.response.defer()

        try:
            issue = await service.reopen_issue(issue_number)
        except Exception as error:
            return await interaction.followup.send(f"Failed to reopen issue: {error}")

        view = await self._load_issue_board_view(
            repo=repo,
            issue_number=issue_number,
            issue_state="open",
        )
        await interaction.message.edit(embed=_build_issue_embed(issue, owner, repo), view=view)
        await interaction.followup.send(f"Issue #{issue_number} reopened by {interaction.user.mention}.")

    async def _handle_comment(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can comment.")

        _, owner, repo, issue_number_raw = custom_id.split(":")
        issue_number = int(issue_number_raw)
        service = _get_github_service(repo)

        modal = AddCommentModal(issue_number)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not modal.comment:
            return

        try:
            await service.add_issue_comment(issue_number, modal.comment)
            issue = await service.get_issue(issue_number)
        except Exception as error:
            return await interaction.followup.send(f"Failed to add comment: {error}")

        view = await self._load_issue_board_view(
            repo=repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await interaction.message.edit(embed=_build_issue_embed(issue, owner, repo), view=view)
        await interaction.followup.send(f"Comment added to issue #{issue_number} by {interaction.user.mention}.")

    async def _handle_pr_merge(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can merge PRs.")

        _, owner, repo, pr_number_raw = custom_id.split(":")
        pr_number = int(pr_number_raw)
        service = _get_github_service(repo)

        confirm_view = MergeConfirmView()
        await interaction.response.send_message(
            f"Merge PR #{pr_number}. Select a merge method and confirm:",
            view=confirm_view,
        )
        await confirm_view.wait()
        if not confirm_view.confirmed or not confirm_view.merge_method:
            return await interaction.followup.send("Merge cancelled.")

        try:
            await service.merge_pr(pr_number, merge_method=confirm_view.merge_method)
            pr = await service.get_pr(pr_number)
        except Exception as error:
            return await interaction.followup.send(f"Failed to merge PR: {error}")

        view = await self._load_pr_board_view(
            repo=repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=True,
        )
        await interaction.message.edit(embed=_build_pr_embed(pr, owner, repo), view=view)
        await interaction.followup.send(
            f"PR #{pr_number} merged via **{confirm_view.merge_method}** by {interaction.user.mention}."
        )

    async def _handle_pr_close(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can close PRs.")

        _, owner, repo, pr_number_raw = custom_id.split(":")
        pr_number = int(pr_number_raw)
        service = _get_github_service(repo)
        await interaction.response.defer()

        try:
            pr = await service.close_pr(pr_number)
        except Exception as error:
            return await interaction.followup.send(f"Failed to close PR: {error}")

        view = await self._load_pr_board_view(
            repo=repo,
            pr_number=pr_number,
            pr_state="closed",
            merged=False,
        )
        await interaction.message.edit(embed=_build_pr_embed(pr, owner, repo), view=view)
        await interaction.followup.send(f"PR #{pr_number} closed by {interaction.user.mention}.")

    async def _handle_pr_reopen(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can reopen PRs.")

        _, owner, repo, pr_number_raw = custom_id.split(":")
        pr_number = int(pr_number_raw)
        service = _get_github_service(repo)
        await interaction.response.defer()

        try:
            pr = await service.reopen_pr(pr_number)
        except Exception as error:
            return await interaction.followup.send(f"Failed to reopen PR: {error}")

        view = await self._load_pr_board_view(
            repo=repo,
            pr_number=pr_number,
            pr_state="open",
            merged=False,
        )
        await interaction.message.edit(embed=_build_pr_embed(pr, owner, repo), view=view)
        await interaction.followup.send(f"PR #{pr_number} reopened by {interaction.user.mention}.")

    async def _handle_pr_comment(self, interaction: discord.Interaction, custom_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can comment on PRs.")

        _, owner, repo, pr_number_raw = custom_id.split(":")
        pr_number = int(pr_number_raw)
        service = _get_github_service(repo)

        modal = PRCommentModal(pr_number)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not modal.comment:
            return

        try:
            await service.add_pr_comment(pr_number, modal.comment)
            pr = await service.get_pr(pr_number)
        except Exception as error:
            return await interaction.followup.send(f"Failed to add comment: {error}")

        view = await self._load_pr_board_view(
            repo=repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=pr.get("merged", False),
        )
        await interaction.message.edit(embed=_build_pr_embed(pr, owner, repo), view=view)
        await interaction.followup.send(f"Comment added to PR #{pr_number} by {interaction.user.mention}.")


def setup(bot: discord.Bot):
    bot.add_cog(GitHubCog(bot))
