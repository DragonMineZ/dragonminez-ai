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
    LabelSelectView,
    MergeConfirmView,
    ModalPrompt,
    issue_board_items,
    pr_board_items,
    PRCommentModal,
)
from bulmaai.utils.permissions import is_staff
from bulmaai.web.core import Tier, tier_for
from bulmaai.ui.v2 import card, facts

log = logging.getLogger(__name__)
settings = load_settings()


async def repo_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    current = (ctx.value or "").lower()
    return [repo for repo in settings.GITHUB_REPOS if current in repo.lower()][:25]


class UnknownRepoError(ValueError):
    pass


def _get_github_service(repo: str | None = None) -> GitHubService:
    target_repo = repo or settings.GITHUB_DEFAULT_REPO
    if target_repo not in settings.GITHUB_REPOS:
        raise UnknownRepoError(f"`{target_repo}` isn't one of the configured repositories.")
    auth = GitHubAppAuth(
        app_id=settings.GH_APP_ID,
        installation_id=settings.GH_INSTALLATION_ID,
        private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM,
    )
    whitelist_path = settings.GITHUB_WHITELIST_FILE_PATH if target_repo == settings.GITHUB_WHITELIST_REPO else None
    return GitHubService(
        auth=auth,
        owner=settings.GITHUB_OWNER,
        repo=target_repo,
        base_branch=settings.GITHUB_BASE_BRANCH,
        whitelist_file_path=whitelist_path,
    )


def _labels(item: dict) -> str:
    return " ".join(f"`{label['name']}`" for label in item.get("labels", [])[:10])


def _body(item: dict) -> str:
    body = item.get("body") or "No description"
    return discord.utils.escape_markdown(body[:1500] + "..." if len(body) > 1500 else body)


def _gh_card(top: str, details: str, body: str, color, *, buttons=None, note: str | None = None):
    """Issue/PR card: an optional result line on top, the linked title + facts, then the description."""
    return card(
        f"-# {note}" if note else None,
        top + (f"\n{details}" if details else ""),
        discord.ui.Separator(),
        body,
        color=color,
        buttons=buttons,
    )


def _issue_card(issue: dict, owner: str, repo: str, *, buttons=None, note: str | None = None):
    is_open = issue["state"] == "open"
    title = discord.utils.escape_markdown(issue["title"])
    top = f"-# {owner}/{repo} · Issue\n## [{'🟢' if is_open else '🔴'} #{issue['number']} {title}]({issue['html_url']})"
    details = facts(
        ("State", issue["state"].title()),
        ("Assignees", ", ".join(assignee["login"] for assignee in issue.get("assignees", [])[:5])),
        ("Labels", _labels(issue)),
    )
    color = discord.Color.green() if is_open else discord.Color.red()
    return _gh_card(top, details, _body(issue), color, buttons=buttons, note=note)


def _list_card(heading: str, lines: list[str], total: int, empty: str, *, buttons=None, hint: str):
    shown = f"Showing 15 of {total} · " if total > 15 else ""
    return card(
        f"## {heading}",
        "\n".join(lines) or empty,
        f"-# {shown}{hint}",
        color=discord.Color.blurple(),
        buttons=buttons,
    )


def _issue_list_card(issues: list[dict], owner: str, repo: str, state: str, *, buttons=None):
    lines = []
    for issue in issues[:15]:
        state_emoji = "🟢" if issue["state"] == "open" else "🔴"
        labels = " ".join(f"`{label['name']}`" for label in issue.get("labels", [])[:3])
        title = discord.utils.escape_markdown(issue["title"][:50])
        lines.append(f"{state_emoji} **#{issue['number']}** [{title}]({issue['html_url']}) {labels}")
    return _list_card(
        f"📋 {state.title()} issues · {owner}/{repo}", lines, len(issues), "No issues found.",
        buttons=buttons, hint="Pick an issue in the dropdown to open it here.",
    )


def _pr_state(pr: dict) -> tuple[str, discord.Color, str]:
    if pr.get("merged", False):
        return "🟣", discord.Color.purple(), "Merged"
    if pr["state"] == "open":
        draft = pr.get("draft", False)
        return ("📝", discord.Color.dark_grey(), "Draft") if draft else ("🟢", discord.Color.green(), "Open")
    return "🔴", discord.Color.red(), "Closed"


def _pr_card(pr: dict, owner: str, repo: str, *, buttons=None, note: str | None = None):
    emoji, color, state_text = _pr_state(pr)
    title = discord.utils.escape_markdown(pr["title"])
    top = f"-# {owner}/{repo} · Pull request\n## [{emoji} #{pr['number']} {title}]({pr['html_url']})"
    changes = None
    if pr.get("additions") is not None:
        changes = f"+{pr['additions']} / -{pr['deletions']}"
        if pr.get("changed_files") is not None:
            changes += f" in {pr['changed_files']} file(s)"
    details = "\n".join(
        line
        for line in (
            facts(
                ("State", state_text),
                ("Branch", f"`{pr['head']['ref']}` → `{pr['base']['ref']}`"),
                ("Author", pr["user"]["login"] if pr.get("user") else None),
            ),
            facts(
                ("Changes", changes),
                ("Mergeable", pr["mergeable_state"].replace("_", " ").title() if pr.get("mergeable_state") else None),
                ("Reviewers", ", ".join(reviewer["login"] for reviewer in pr.get("requested_reviewers", [])[:5])),
            ),
            facts(("Labels", _labels(pr))),
        )
        if line
    )
    return _gh_card(top, details, _body(pr), color, buttons=buttons, note=note)


def _pr_list_card(prs: list[dict], owner: str, repo: str, state: str, *, buttons=None):
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
        title = discord.utils.escape_markdown(pr["title"][:50])
        lines.append(f"{emoji} **#{pr['number']}** [{title}]({pr['html_url']}) by `{pr['user']['login']}`")
    return _list_card(
        f"📋 {state.title()} pull requests · {owner}/{repo}", lines, len(prs), "No pull requests found.",
        buttons=buttons, hint="Pick a PR in the dropdown to open it here.",
    )


class GitHubCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.owner = settings.GITHUB_OWNER
        self.default_repo = settings.GITHUB_DEFAULT_REPO

    github = discord.SlashCommandGroup("github", "GitHub issue management commands")

    async def _service(self, ctx, repo: str | None) -> GitHubService | None:
        """ctx is an ApplicationContext or an Interaction; None once they've been told the repo isn't allowed."""
        try:
            return _get_github_service(repo)
        except UnknownRepoError as error:
            await ctx.respond(str(error), ephemeral=True)
            return None

    def _is_github_admin(self, member) -> bool:
        # Merging and closing/reopening PRs need panel ADMIN; staff keep issues and PR comments.
        return tier_for(member, self.bot.settings) >= Tier.ADMIN

    async def _load_issue_board_view(
        self,
        *,
        repo: str,
        issue_number: int,
        issue_state: str,
    ) -> list[discord.ui.Item]:
        service = _get_github_service(repo)
        issues = await service.list_issues(state="all")
        issues = [issue for issue in issues if "pull_request" not in issue]
        return issue_board_items(
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
    ) -> list[discord.ui.Item]:
        service = _get_github_service(repo)
        prs = await service.list_prs(state="all")
        return pr_board_items(
            prs=prs or [{"number": pr_number, "title": f"PR #{pr_number}", "user": {"login": "unknown"}}],
            owner=self.owner,
            repo=repo,
            pr_number=pr_number,
            pr_state=pr_state,
            merged=merged,
        )

    async def _prompt_issue_modal(self, ctx: discord.ApplicationContext, modal: CreateIssueModal) -> dict | None:
        prompt = ModalPrompt(
            "**Step 2/2:** Click to enter the issue details.", "Enter Issue Details", modal, author_id=ctx.author.id
        )
        prompt_message = await ctx.followup.send(view=prompt)
        await modal.wait()
        await prompt_message.edit(view=card("Issue details submitted."))
        return modal.result

    @github.command(name="create", description="Create a new GitHub issue with labels")
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def create_issue(self, ctx: discord.ApplicationContext, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can create issues.")

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            labels = await service.get_labels()
        except Exception as error:
            log.exception("Failed to fetch labels")
            return await ctx.followup.send(f"Failed to fetch labels: {error}")

        label_view = LabelSelectView(
            labels, text="**Step 1/2:** Select labels for the new issue or skip them.", author_id=ctx.author.id
        )
        selection_message = await ctx.followup.send(view=label_view)
        await label_view.wait()
        if not label_view.confirmed:
            await selection_message.edit(view=card("Issue creation cancelled."))
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

        await selection_message.edit(view=card("Issue created."))
        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue["number"],
            issue_state="open",
        )
        await ctx.followup.send(view=_issue_card(issue, self.owner, target_repo, buttons=view, note="Issue created successfully."))

    @github.command(name="close", description="Close a GitHub issue")
    @discord.option("issue_number", description="Issue number to close", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def close_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can close issues.")

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
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

        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state="closed",
        )
        await ctx.followup.send(view=_issue_card(issue, self.owner, target_repo, buttons=view, note=f"Issue #{issue_number} closed by {ctx.author.mention}."))

    @github.command(name="reopen", description="Reopen a closed GitHub issue")
    @discord.option("issue_number", description="Issue number to reopen", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def reopen_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can reopen issues.")

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            issue = await service.reopen_issue(issue_number)
        except Exception as error:
            log.exception("Failed to reopen issue")
            return await ctx.followup.send(f"Failed to reopen issue: {error}")

        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state="open",
        )
        await ctx.followup.send(view=_issue_card(issue, self.owner, target_repo, buttons=view, note=f"Issue #{issue_number} reopened by {ctx.author.mention}."))

    @github.command(name="view", description="View a GitHub issue")
    @discord.option("issue_number", description="Issue number to view", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def view_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            issue = await service.get_issue(issue_number)
        except Exception as error:
            log.exception("Failed to fetch issue")
            return await ctx.followup.send(f"Failed to fetch issue: {error}")

        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await ctx.followup.send(view=_issue_card(issue, self.owner, target_repo, buttons=view))

    @github.command(name="list", description="List GitHub issues")
    @discord.option("state", description="Issue state", choices=["open", "closed", "all"], required=False)
    @discord.option("label", description="Filter by label", required=False)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def list_issues(self, ctx: discord.ApplicationContext, state: str = "open", label: str = None, repo: str = None):
        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            issues = await service.list_issues(state=state, labels=label)
        except Exception as error:
            log.exception("Failed to list issues")
            return await ctx.followup.send(f"Failed to list issues: {error}")

        issues = [issue for issue in issues if "pull_request" not in issue]
        if not issues:
            return await ctx.followup.send(f"No {state} issues found.")

        view = issue_board_items(issues=issues, owner=self.owner, repo=target_repo)
        await ctx.followup.send(view=_issue_list_card(issues, self.owner, target_repo, state, buttons=view))

    @github.command(name="comment", description="Add a comment to a GitHub issue")
    @discord.option("issue_number", description="Issue number", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def add_comment(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can comment on issues.")

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
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

        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await ctx.followup.send(view=_issue_card(issue, self.owner, target_repo, buttons=view, note=f"Comment added to issue #{issue_number} by {ctx.author.mention}."))

    @github.command(name="labels", description="View available labels for a repository")
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def list_labels(self, ctx: discord.ApplicationContext, repo: str = None):
        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            labels = await service.get_labels()
        except Exception as error:
            log.exception("Failed to fetch labels")
            return await ctx.followup.send(f"Failed to fetch labels: {error}")

        if not labels:
            return await ctx.followup.send("No labels found.")

        lines = "\n".join(
            f"• **{label['name']}** `#{label['color']}`" +
            (f" · {label['description'][:50]}" if label.get("description") else "")
            for label in labels[:25]
        )
        title = f"## 🏷️ Labels · {self.owner}/{target_repo}"
        await ctx.followup.send(view=card(title, lines, color=discord.Color.blurple()))

    @github.command(name="addlabel", description="Add a label to an issue")
    @discord.option("issue_number", description="Issue number", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def add_label_to_issue(self, ctx: discord.ApplicationContext, issue_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can add labels.")

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            labels = await service.get_labels()
        except Exception as error:
            return await ctx.followup.send(f"Failed to fetch labels: {error}")

        label_view = LabelSelectView(labels, text=f"Select labels to add to issue #{issue_number}:", author_id=ctx.author.id)
        prompt_message = await ctx.followup.send(view=label_view)
        await label_view.wait()
        if not label_view.confirmed or not label_view.selected_labels:
            await prompt_message.edit(view=card("No labels selected."))
            return

        try:
            await service.add_labels(issue_number, label_view.selected_labels)
            issue = await service.get_issue(issue_number)
        except Exception as error:
            return await ctx.followup.send(f"Failed to add labels: {error}")

        await prompt_message.edit(view=card("Labels updated."))
        view = await self._load_issue_board_view(
            repo=target_repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await ctx.followup.send(view=_issue_card(issue, self.owner, target_repo, buttons=view, note=f"Labels added to issue #{issue_number}."))

    pr = github.create_subgroup("pr", "GitHub pull request management")

    @pr.command(name="list", description="List pull requests")
    @discord.option("state", description="PR state", choices=["open", "closed", "all"], required=False)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def list_prs(self, ctx: discord.ApplicationContext, state: str = "open", repo: str = None):
        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            prs = await service.list_prs(state=state)
        except Exception as error:
            log.exception("Failed to list PRs")
            return await ctx.followup.send(f"Failed to list pull requests: {error}")

        if not prs:
            return await ctx.followup.send(f"No {state} pull requests found.")

        view = pr_board_items(prs=prs, owner=self.owner, repo=target_repo)
        await ctx.followup.send(view=_pr_list_card(prs, self.owner, target_repo, state, buttons=view))

    @pr.command(name="view", description="View a pull request")
    @discord.option("pr_number", description="PR number to view", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def view_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            pr = await service.get_pr(pr_number)
        except Exception as error:
            log.exception("Failed to fetch PR")
            return await ctx.followup.send(f"Failed to fetch pull request: {error}")

        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=pr.get("merged", False),
        )
        await ctx.followup.send(view=_pr_card(pr, self.owner, target_repo, buttons=view))

    @pr.command(name="merge", description="Merge a pull request")
    @discord.option("pr_number", description="PR number to merge", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def merge_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        if not self._is_github_admin(ctx.author):
            return await ctx.respond("Only admins can merge pull requests.", ephemeral=True)

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        confirm_view = MergeConfirmView(
            text=f"Merge PR #{pr_number}. Select a merge method and confirm:", author_id=ctx.author.id
        )
        prompt_message = await ctx.followup.send(view=confirm_view)
        await confirm_view.wait()
        if not confirm_view.confirmed or not confirm_view.merge_method:
            await prompt_message.edit(view=card("Merge cancelled."))
            return

        try:
            await service.merge_pr(pr_number, merge_method=confirm_view.merge_method)
            pr = await service.get_pr(pr_number)
        except Exception as error:
            log.exception("Failed to merge PR")
            return await ctx.followup.send(f"Failed to merge pull request: {error}")

        await prompt_message.edit(view=card("Merge completed."))
        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=True,
        )
        await ctx.followup.send(view=_pr_card(pr, self.owner, target_repo, buttons=view, note=f"PR #{pr_number} merged via **{confirm_view.merge_method}** by {ctx.author.mention}."))

    @pr.command(name="close", description="Close a pull request")
    @discord.option("pr_number", description="PR number to close", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def close_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        if not self._is_github_admin(ctx.author):
            return await ctx.respond("Only admins can close pull requests.", ephemeral=True)

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            pr = await service.close_pr(pr_number)
        except Exception as error:
            log.exception("Failed to close PR")
            return await ctx.followup.send(f"Failed to close pull request: {error}")

        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state="closed",
            merged=False,
        )
        await ctx.followup.send(view=_pr_card(pr, self.owner, target_repo, buttons=view, note=f"PR #{pr_number} closed by {ctx.author.mention}."))

    @pr.command(name="reopen", description="Reopen a closed pull request")
    @discord.option("pr_number", description="PR number to reopen", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def reopen_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        if not self._is_github_admin(ctx.author):
            return await ctx.respond("Only admins can reopen pull requests.", ephemeral=True)

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
        await ctx.defer()

        try:
            pr = await service.reopen_pr(pr_number)
        except Exception as error:
            log.exception("Failed to reopen PR")
            return await ctx.followup.send(f"Failed to reopen pull request: {error}")

        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state="open",
            merged=False,
        )
        await ctx.followup.send(view=_pr_card(pr, self.owner, target_repo, buttons=view, note=f"PR #{pr_number} reopened by {ctx.author.mention}."))

    @pr.command(name="comment", description="Add a comment to a pull request")
    @discord.option("pr_number", description="PR number", required=True)
    @discord.option("repo", description="Repository name", autocomplete=repo_autocomplete, required=False)
    async def comment_pr(self, ctx: discord.ApplicationContext, pr_number: int, repo: str = None):
        if not is_staff(ctx.author):
            return await ctx.respond("Only staff can comment on pull requests.")

        target_repo = repo or self.default_repo
        if (service := await self._service(ctx, target_repo)) is None:
            return
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

        view = await self._load_pr_board_view(
            repo=target_repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=pr.get("merged", False),
        )
        await ctx.followup.send(view=_pr_card(pr, self.owner, target_repo, buttons=view, note=f"Comment added to PR #{pr_number} by {ctx.author.mention}."))

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "")
        try:
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
        except UnknownRepoError as error:
            await interaction.respond(str(error), ephemeral=True)

    async def _handle_issue_select(self, interaction: discord.Interaction, custom_id: str):
        parts = custom_id.split(":")
        owner, repo = parts[1], parts[2]
        issue_number = int(interaction.data.get("values", [None])[0])
        service = _get_github_service(repo)

        issue = await service.get_issue(issue_number)
        issues = [item for item in await service.list_issues(state="all") if "pull_request" not in item]
        view = issue_board_items(
            issues=issues,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            issue_state=issue["state"],
        )
        await interaction.response.edit_message(view=_issue_card(issue, owner, repo, buttons=view))

    async def _handle_pr_select(self, interaction: discord.Interaction, custom_id: str):
        parts = custom_id.split(":")
        owner, repo = parts[1], parts[2]
        pr_number = int(interaction.data.get("values", [None])[0])
        service = _get_github_service(repo)

        pr = await service.get_pr(pr_number)
        prs = await service.list_prs(state="all")
        view = pr_board_items(
            prs=prs,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            pr_state=pr["state"],
            merged=pr.get("merged", False),
        )
        await interaction.response.edit_message(view=_pr_card(pr, owner, repo, buttons=view))

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
        await interaction.message.edit(view=_issue_card(issue, owner, repo, buttons=view))
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
        await interaction.message.edit(view=_issue_card(issue, owner, repo, buttons=view))
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
        await interaction.message.edit(view=_issue_card(issue, owner, repo, buttons=view))
        await interaction.followup.send(f"Comment added to issue #{issue_number} by {interaction.user.mention}.")

    async def _handle_pr_merge(self, interaction: discord.Interaction, custom_id: str):
        if not self._is_github_admin(interaction.user):
            return await interaction.response.send_message("Only admins can merge PRs.", ephemeral=True)

        _, owner, repo, pr_number_raw = custom_id.split(":")
        pr_number = int(pr_number_raw)
        service = _get_github_service(repo)

        confirm_view = MergeConfirmView(
            text=f"Merge PR #{pr_number}. Select a merge method and confirm:", author_id=interaction.user.id
        )
        await interaction.response.send_message(view=confirm_view)
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
        await interaction.message.edit(view=_pr_card(pr, owner, repo, buttons=view))
        await interaction.followup.send(
            f"PR #{pr_number} merged via **{confirm_view.merge_method}** by {interaction.user.mention}."
        )

    async def _handle_pr_close(self, interaction: discord.Interaction, custom_id: str):
        if not self._is_github_admin(interaction.user):
            return await interaction.response.send_message("Only admins can close PRs.", ephemeral=True)

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
        await interaction.message.edit(view=_pr_card(pr, owner, repo, buttons=view))
        await interaction.followup.send(f"PR #{pr_number} closed by {interaction.user.mention}.")

    async def _handle_pr_reopen(self, interaction: discord.Interaction, custom_id: str):
        if not self._is_github_admin(interaction.user):
            return await interaction.response.send_message("Only admins can reopen PRs.", ephemeral=True)

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
        await interaction.message.edit(view=_pr_card(pr, owner, repo, buttons=view))
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
        await interaction.message.edit(view=_pr_card(pr, owner, repo, buttons=view))
        await interaction.followup.send(f"Comment added to PR #{pr_number} by {interaction.user.mention}.")


def setup(bot: discord.Bot):
    bot.add_cog(GitHubCog(bot))
