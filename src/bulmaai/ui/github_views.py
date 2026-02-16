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
        self.add_item(self.title_input)

        self.body_input = discord.ui.InputText(
            label="Description",
            placeholder="Detailed description, steps to reproduce, expected behavior...",
            style=discord.InputTextStyle.long,
            min_length=10,
            max_length=4000,
            required=True,
        )
        self.add_item(self.body_input)

    async def callback(self, interaction: discord.Interaction):
        self.result = {
            "title": self.title_input.value.strip(),
            "body": self.body_input.value.strip(),
            "labels": self.selected_labels,
        }
        await interaction.response.defer(ephemeral=True)


class AddCommentModal(discord.ui.Modal):
    def __init__(self, issue_number: int):
        super().__init__(title=f"Add Comment to Issue #{issue_number}")
        self.issue_number = issue_number
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
        await interaction.response.defer(ephemeral=True)


class CloseReasonModal(discord.ui.Modal):
    def __init__(self, issue_number: int):
        super().__init__(title=f"Close Issue #{issue_number}")
        self.issue_number = issue_number
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
        self.add_item(self.reason_input)

        self.comment_input = discord.ui.InputText(
            label="Closing Comment (Optional)",
            placeholder="Reason for closing...",
            style=discord.InputTextStyle.long,
            max_length=2000,
            required=False,
        )
        self.add_item(self.comment_input)

    async def callback(self, interaction: discord.Interaction):
        reason = self.reason_input.value.strip().lower()
        self.reason = reason if reason in ("completed", "not_planned", "duplicate") else "completed"
        self.comment = self.comment_input.value.strip() if self.comment_input.value else None
        await interaction.response.defer(ephemeral=True)


class LabelSelectView(discord.ui.View):
    def __init__(self, labels: list[dict], *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.selected_labels: list[str] = []
        self.confirmed = False

        options = []
        for label in labels[:25]:
            desc = label.get("description", "")[:100] if label.get("description") else None
            options.append(discord.SelectOption(
                label=label["name"][:100],
                value=label["name"],
                description=desc,
            ))

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
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, row=1)
    async def continue_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.confirmed = True
        self.stop()
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.confirmed = False
        self.stop()
        await interaction.response.defer(ephemeral=True)


class IssueManagementView(discord.ui.View):
    def __init__(self, *, issue_number: int, owner: str, repo: str, issue_state: str = "open"):
        super().__init__(timeout=None)
        self.issue_number = issue_number
        self.owner = owner
        self.repo = repo
        self.issue_state = issue_state
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()

        if self.issue_state == "open":
            close_btn = discord.ui.Button(
                label="Close Issue",
                style=discord.ButtonStyle.danger,
                custom_id=f"gh_close:{self.owner}:{self.repo}:{self.issue_number}",
                emoji="🔒",
            )
            self.add_item(close_btn)
        else:
            reopen_btn = discord.ui.Button(
                label="Reopen Issue",
                style=discord.ButtonStyle.success,
                custom_id=f"gh_reopen:{self.owner}:{self.repo}:{self.issue_number}",
                emoji="🔓",
            )
            self.add_item(reopen_btn)

        comment_btn = discord.ui.Button(
            label="Add Comment",
            style=discord.ButtonStyle.primary,
            custom_id=f"gh_comment:{self.owner}:{self.repo}:{self.issue_number}",
            emoji="💬",
        )
        self.add_item(comment_btn)

        link_btn = discord.ui.Button(
            label="View on GitHub",
            style=discord.ButtonStyle.link,
            url=f"https://github.com/{self.owner}/{self.repo}/issues/{self.issue_number}",
            emoji="🔗",
        )
        self.add_item(link_btn)


class PersistentIssueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="placeholder", custom_id="gh_close_placeholder", style=discord.ButtonStyle.secondary)
    async def placeholder(self, button: discord.ui.Button, interaction: discord.Interaction):
        pass


class QuickIssueSelect(discord.ui.View):
    def __init__(self, issues: list[dict], *, owner: str, repo: str, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.selected_issue: dict | None = None
        self.owner = owner
        self.repo = repo

        options = []
        for issue in issues[:25]:
            title = issue["title"][:95] + "..." if len(issue["title"]) > 95 else issue["title"]
            labels = ", ".join(l["name"] for l in issue.get("labels", [])[:3])
            desc = f"#{issue['number']} - {labels}" if labels else f"#{issue['number']}"
            options.append(discord.SelectOption(
                label=title,
                value=str(issue["number"]),
                description=desc[:100],
            ))

        if options:
            select = discord.ui.Select(
                placeholder="Select an issue",
                options=options,
                custom_id="quick_issue_select_temp",
            )
            select.callback = self.select_callback
            self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        issue_num = interaction.data.get("values", [None])[0]
        if issue_num:
            self.selected_issue = {"number": int(issue_num)}
        self.stop()
        await interaction.response.defer(ephemeral=True)


