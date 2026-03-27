import discord


class CreateIssueModal(discord.ui.Modal):
    def __init__(self, selected_labels: list[str] | None = None):
        super().__init__(title="Create GitHub Issue")
        self.selected_labels = selected_labels or []
        self.result: dict | None = None

        self.title_input = discord.ui.InputText(
            label="Issue Title",
            placeholder="Brief description of the issue",
            min_length=5,
            max_length=256,
            required=True,
        )
        self.body_input = discord.ui.InputText(
            label="Description",
            placeholder="Detailed description, steps to reproduce, expected behavior...",
            style=discord.InputTextStyle.long,
            min_length=10,
            max_length=4000,
            required=True,
        )
        self.add_item(self.title_input)
        self.add_item(self.body_input)

    async def callback(self, interaction: discord.Interaction):
        self.result = {
            "title": self.title_input.value.strip(),
            "body": self.body_input.value.strip(),
            "labels": self.selected_labels,
        }
        await interaction.response.defer()


class AddCommentModal(discord.ui.Modal):
    def __init__(self, issue_number: int):
        super().__init__(title=f"Add Comment to Issue #{issue_number}")
        self.comment: str | None = None

        self.comment_input = discord.ui.InputText(
            label="Comment",
            placeholder="Your comment...",
            style=discord.InputTextStyle.long,
            min_length=1,
            max_length=4000,
            required=True,
        )
        self.add_item(self.comment_input)

    async def callback(self, interaction: discord.Interaction):
        self.comment = self.comment_input.value.strip()
        await interaction.response.defer()


class CloseReasonModal(discord.ui.Modal):
    def __init__(self, issue_number: int):
        super().__init__(title=f"Close Issue #{issue_number}")
        self.reason: str | None = None
        self.comment: str | None = None

        self.reason_input = discord.ui.InputText(
            label="Close Reason",
            placeholder="completed, not_planned, or duplicate",
            min_length=1,
            max_length=20,
            required=True,
            value="completed",
        )
        self.comment_input = discord.ui.InputText(
            label="Closing Comment (Optional)",
            placeholder="Reason for closing...",
            style=discord.InputTextStyle.long,
            max_length=2000,
            required=False,
        )
        self.add_item(self.reason_input)
        self.add_item(self.comment_input)

    async def callback(self, interaction: discord.Interaction):
        reason = self.reason_input.value.strip().lower()
        self.reason = reason if reason in ("completed", "not_planned", "duplicate") else "completed"
        self.comment = self.comment_input.value.strip() if self.comment_input.value else None
        await interaction.response.defer()


class LabelSelectView(discord.ui.View):
    def __init__(self, labels: list[dict], *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.selected_labels: list[str] = []
        self.confirmed = False

        options = []
        for label in labels[:25]:
            description = label.get("description", "")[:100] if label.get("description") else None
            options.append(
                discord.SelectOption(
                    label=label["name"][:100],
                    value=label["name"],
                    description=description,
                )
            )

        if options:
            select = discord.ui.Select(
                placeholder="Select labels (optional)",
                options=options,
                min_values=0,
                max_values=min(len(options), 25),
                custom_id="label_select_temp",
            )
            select.callback = self.select_callback
            self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_labels = interaction.data.get("values", [])
        await interaction.response.defer()

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, row=1)
    async def continue_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


def _issue_select_options(issues: list[dict]) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for issue in issues[:25]:
        title = issue["title"][:95] + "..." if len(issue["title"]) > 95 else issue["title"]
        labels = ", ".join(label["name"] for label in issue.get("labels", [])[:3])
        description = f"#{issue['number']} - {labels}" if labels else f"#{issue['number']}"
        options.append(
            discord.SelectOption(
                label=title,
                value=str(issue["number"]),
                description=description[:100],
            )
        )
    return options


class IssueBoardView(discord.ui.View):
    def __init__(
        self,
        *,
        issues: list[dict],
        owner: str,
        repo: str,
        issue_number: int | None = None,
        issue_state: str = "open",
    ):
        super().__init__(timeout=None)
        self.owner = owner
        self.repo = repo

        options = _issue_select_options(issues)
        if options:
            self.add_item(
                discord.ui.Select(
                    placeholder="Switch to another issue",
                    options=options,
                    custom_id=f"gh_issue_select:{owner}:{repo}",
                )
            )

        if issue_number is None:
            return

        if issue_state == "open":
            self.add_item(
                discord.ui.Button(
                    label="Close Issue",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"gh_close:{owner}:{repo}:{issue_number}",
                    emoji="🔒",
                    row=1,
                )
            )
        else:
            self.add_item(
                discord.ui.Button(
                    label="Reopen Issue",
                    style=discord.ButtonStyle.success,
                    custom_id=f"gh_reopen:{owner}:{repo}:{issue_number}",
                    emoji="🔓",
                    row=1,
                )
            )

        self.add_item(
            discord.ui.Button(
                label="Add Comment",
                style=discord.ButtonStyle.primary,
                custom_id=f"gh_comment:{owner}:{repo}:{issue_number}",
                emoji="💬",
                row=1,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="View on GitHub",
                style=discord.ButtonStyle.link,
                url=f"https://github.com/{owner}/{repo}/issues/{issue_number}",
                emoji="🔗",
                row=1,
            )
        )


class PRCommentModal(discord.ui.Modal):
    def __init__(self, pr_number: int):
        super().__init__(title=f"Comment on PR #{pr_number}")
        self.comment: str | None = None

        self.comment_input = discord.ui.InputText(
            label="Comment",
            placeholder="Your comment on this pull request...",
            style=discord.InputTextStyle.long,
            min_length=1,
            max_length=4000,
            required=True,
        )
        self.add_item(self.comment_input)

    async def callback(self, interaction: discord.Interaction):
        self.comment = self.comment_input.value.strip()
        await interaction.response.defer()


class MergeConfirmView(discord.ui.View):
    def __init__(self, *, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.merge_method: str | None = None
        self.confirmed = False

        select = discord.ui.Select(
            placeholder="Select merge method",
            options=[
                discord.SelectOption(label="Squash and Merge", value="squash", description="Squash all commits into one", emoji="🔹"),
                discord.SelectOption(label="Merge Commit", value="merge", description="Create a merge commit", emoji="🔸"),
                discord.SelectOption(label="Rebase and Merge", value="rebase", description="Rebase commits onto base", emoji="🔻"),
            ],
            custom_id="merge_method_select_temp",
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        self.merge_method = interaction.data.get("values", [None])[0]
        await interaction.response.defer()

    @discord.ui.button(label="Confirm Merge", style=discord.ButtonStyle.success, row=1, emoji="✅")
    async def confirm_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not self.merge_method:
            return await interaction.response.send_message("Please select a merge method first.")
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


def _pr_select_options(prs: list[dict]) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for pr in prs[:25]:
        title = pr["title"][:95] + "..." if len(pr["title"]) > 95 else pr["title"]
        description = f"#{pr['number']} by {pr['user']['login']}"
        if pr.get("draft"):
            description += " (draft)"
        options.append(
            discord.SelectOption(
                label=title,
                value=str(pr["number"]),
                description=description[:100],
            )
        )
    return options


class PRBoardView(discord.ui.View):
    def __init__(
        self,
        *,
        prs: list[dict],
        owner: str,
        repo: str,
        pr_number: int | None = None,
        pr_state: str = "open",
        merged: bool = False,
    ):
        super().__init__(timeout=None)
        self.owner = owner
        self.repo = repo

        options = _pr_select_options(prs)
        if options:
            self.add_item(
                discord.ui.Select(
                    placeholder="Switch to another pull request",
                    options=options,
                    custom_id=f"gh_pr_select:{owner}:{repo}",
                )
            )

        if pr_number is None:
            return

        if pr_state == "open":
            self.add_item(
                discord.ui.Button(
                    label="Merge PR",
                    style=discord.ButtonStyle.success,
                    custom_id=f"gh_pr_merge:{owner}:{repo}:{pr_number}",
                    emoji="✅",
                    row=1,
                )
            )
            self.add_item(
                discord.ui.Button(
                    label="Close PR",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"gh_pr_close:{owner}:{repo}:{pr_number}",
                    emoji="🔒",
                    row=1,
                )
            )
        elif not merged:
            self.add_item(
                discord.ui.Button(
                    label="Reopen PR",
                    style=discord.ButtonStyle.success,
                    custom_id=f"gh_pr_reopen:{owner}:{repo}:{pr_number}",
                    emoji="🔓",
                    row=1,
                )
            )

        self.add_item(
            discord.ui.Button(
                label="Comment",
                style=discord.ButtonStyle.primary,
                custom_id=f"gh_pr_comment:{owner}:{repo}:{pr_number}",
                emoji="💬",
                row=1,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="View on GitHub",
                style=discord.ButtonStyle.link,
                url=f"https://github.com/{owner}/{repo}/pull/{pr_number}",
                emoji="🔗",
                row=1,
            )
        )
