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


class _AuthorOnlyPrompt(discord.ui.DesignerView):
    """A private step card (text + controls) only the command's author can use; wait() until they choose."""

    def __init__(self, text: str, *rows: discord.ui.ActionRow, author_id: int, timeout: float):
        super().__init__(discord.ui.Container(discord.ui.TextDisplay(text), *rows, color=discord.Color.blurple()), timeout=timeout)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Only whoever ran this command can use these controls.", ephemeral=True)
        return False

    @staticmethod
    def _decision_row(confirm_label: str, confirm, cancel, *, emoji: str | None = None) -> discord.ui.ActionRow:
        confirm_button = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.success, emoji=emoji)
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm_button.callback, cancel_button.callback = confirm, cancel
        return discord.ui.ActionRow(confirm_button, cancel_button)


class LabelSelectView(_AuthorOnlyPrompt):
    def __init__(self, labels: list[dict], *, text: str, author_id: int, timeout: float = 300):
        self.selected_labels: list[str] = []
        self.confirmed = False
        rows = []
        options = [
            discord.SelectOption(
                label=label["name"][:100],
                value=label["name"],
                description=label["description"][:100] if label.get("description") else None,
            )
            for label in labels[:25]
        ]
        if options:
            select = discord.ui.Select(
                placeholder="Select labels (optional)", options=options, min_values=0, max_values=len(options)
            )
            select.callback = self.select_callback
            rows.append(discord.ui.ActionRow(select))
        rows.append(self._decision_row("Continue", self._continue, self._cancel))
        super().__init__(text, *rows, author_id=author_id, timeout=timeout)

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_labels = interaction.data.get("values", [])
        await interaction.response.defer()

    async def _continue(self, interaction: discord.Interaction):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    async def _cancel(self, interaction: discord.Interaction):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


class ModalPrompt(_AuthorOnlyPrompt):
    """One button that opens modal; wait on the modal, not on this view."""

    def __init__(self, text: str, label: str, modal: discord.ui.Modal, *, author_id: int, timeout: float = 300):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        button.callback = self._open
        self.modal = modal
        super().__init__(text, discord.ui.ActionRow(button), author_id=author_id, timeout=timeout)

    async def _open(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.modal)


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


def _button(label: str, emoji: str, style: discord.ButtonStyle, custom_id: str) -> discord.ui.Button:
    return discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=custom_id)


def issue_board_items(
    *,
    issues: list[dict],
    owner: str,
    repo: str,
    issue_number: int | None = None,
    issue_state: str = "open",
) -> list[discord.ui.Item]:
    """The board's switcher and the open issue's buttons; GitHubCog.on_interaction routes them by custom id."""
    items: list[discord.ui.Item] = []
    if options := _issue_select_options(issues):
        items.append(
            discord.ui.Select(placeholder="Switch to another issue", options=options, custom_id=f"gh_issue_select:{owner}:{repo}")
        )
    if issue_number is None:
        return items
    target = f"{owner}:{repo}:{issue_number}"
    if issue_state == "open":
        items.append(_button("Close Issue", "🔒", discord.ButtonStyle.danger, f"gh_close:{target}"))
    else:
        items.append(_button("Reopen Issue", "🔓", discord.ButtonStyle.success, f"gh_reopen:{target}"))
    items.append(_button("Add Comment", "💬", discord.ButtonStyle.primary, f"gh_comment:{target}"))
    items.append(
        discord.ui.Button(label="View on GitHub", emoji="🔗", url=f"https://github.com/{owner}/{repo}/issues/{issue_number}")
    )
    return items


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


class MergeConfirmView(_AuthorOnlyPrompt):
    def __init__(self, *, text: str = "Select a merge method and confirm:", author_id: int, timeout: float = 120):
        self.merge_method: str | None = None
        self.confirmed = False
        select = discord.ui.Select(
            placeholder="Select merge method",
            options=[
                discord.SelectOption(label="Squash and Merge", value="squash", description="Squash all commits into one", emoji="🔹"),
                discord.SelectOption(label="Merge Commit", value="merge", description="Create a merge commit", emoji="🔸"),
                discord.SelectOption(label="Rebase and Merge", value="rebase", description="Rebase commits onto base", emoji="🔻"),
            ],
        )
        select.callback = self.select_callback
        super().__init__(
            text,
            discord.ui.ActionRow(select),
            self._decision_row("Confirm Merge", self._confirm, self._cancel, emoji="✅"),
            author_id=author_id,
            timeout=timeout,
        )

    async def select_callback(self, interaction: discord.Interaction):
        self.merge_method = interaction.data.get("values", [None])[0]
        await interaction.response.defer()

    async def _confirm(self, interaction: discord.Interaction):
        if not self.merge_method:
            return await interaction.response.send_message("Please select a merge method first.", ephemeral=True)
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    async def _cancel(self, interaction: discord.Interaction):
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


def pr_board_items(
    *,
    prs: list[dict],
    owner: str,
    repo: str,
    pr_number: int | None = None,
    pr_state: str = "open",
    merged: bool = False,
) -> list[discord.ui.Item]:
    """The board's switcher and the open PR's buttons; GitHubCog.on_interaction routes them by custom id."""
    items: list[discord.ui.Item] = []
    if options := _pr_select_options(prs):
        items.append(
            discord.ui.Select(placeholder="Switch to another pull request", options=options, custom_id=f"gh_pr_select:{owner}:{repo}")
        )
    if pr_number is None:
        return items
    target = f"{owner}:{repo}:{pr_number}"
    if pr_state == "open":
        items.append(_button("Merge PR", "✅", discord.ButtonStyle.success, f"gh_pr_merge:{target}"))
        items.append(_button("Close PR", "🔒", discord.ButtonStyle.danger, f"gh_pr_close:{target}"))
    elif not merged:
        items.append(_button("Reopen PR", "🔓", discord.ButtonStyle.success, f"gh_pr_reopen:{target}"))
    items.append(_button("Comment", "💬", discord.ButtonStyle.primary, f"gh_pr_comment:{target}"))
    items.append(discord.ui.Button(label="View on GitHub", emoji="🔗", url=f"https://github.com/{owner}/{repo}/pull/{pr_number}"))
    return items
