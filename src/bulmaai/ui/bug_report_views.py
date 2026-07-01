import discord

from bulmaai.services.bug_report_ai import BugTriage, DuplicateAssessment

SEVERITY_COLORS = {
    "low": discord.Color.green(),
    "medium": discord.Color.gold(),
    "high": discord.Color.orange(),
    "critical": discord.Color.red(),
}

STATUS_LABELS = {
    "triaged": "🔎 Awaiting staff review",
    "tracked": "🛠️ Tracked — fix in progress",
    "resolved": "✅ Resolved",
    "dismissed": "🚫 Not a bug",
    # Display-only statuses. These reuse the stored 'resolved'/'dismissed' states in the DB
    # but read correctly in the embed.
    "duplicate": "🔁 Closed as duplicate",
    "fixed": "✅ Already fixed",
}

# Display statuses that should recolour the embed like a "closed" outcome.
_GREEN_STATUSES = frozenset({"resolved", "fixed"})
_GREY_STATUSES = frozenset({"dismissed", "duplicate"})


def _duplicate_field(duplicate: DuplicateAssessment) -> tuple[str, str] | None:
    """Render an AI duplicate/already-fixed suggestion as an embed (name, value) pair."""
    if not duplicate.has_match:
        return None
    ref = f"#{duplicate.issue_number} — {duplicate.issue_title}".strip()[:200]
    reason = f"\n{duplicate.reason}" if duplicate.reason else ""
    suffix = f" · confidence: {duplicate.confidence}"
    if duplicate.match_type == "duplicate":
        return "🔁 Possible duplicate", f"{ref}{suffix}{reason}"[:1024]
    return "✅ Possibly already fixed", f"Closed issue {ref}{suffix}{reason}"[:1024]


def build_triage_embed(
    triage: BugTriage,
    *,
    status: str = "triaged",
    reporter_id: int | None = None,
    duplicate: DuplicateAssessment | None = None,
) -> discord.Embed:
    color = SEVERITY_COLORS.get(triage.severity, discord.Color.gold())
    if status in _GREEN_STATUSES:
        color = discord.Color.green()
    elif status in _GREY_STATUSES:
        color = discord.Color.dark_grey()

    embed = discord.Embed(
        title=f"🐛 {triage.title}",
        description=triage.summary or "No summary available.",
        color=color,
    )
    embed.add_field(name="Severity", value=triage.severity.title(), inline=True)
    embed.add_field(name="Affected Area", value=triage.affected_area or "Unknown", inline=True)
    embed.add_field(
        name="Likely a bug?",
        value="Yes" if triage.is_bug else "Unclear / probably not",
        inline=True,
    )
    if triage.steps:
        steps = "\n".join(f"{index}. {step}" for index, step in enumerate(triage.steps[:8], start=1))
        embed.add_field(name="Steps to Reproduce", value=steps[:1024], inline=False)
    if duplicate is not None:
        dup_field = _duplicate_field(duplicate)
        if dup_field is not None:
            embed.add_field(name=dup_field[0], value=dup_field[1], inline=False)
    if reporter_id is not None:
        embed.add_field(name="Reporter", value=f"<@{reporter_id}>", inline=True)
    embed.add_field(name="Status", value=STATUS_LABELS.get(status, status), inline=True)
    embed.set_footer(text="AI-generated triage · staff actions below")
    return embed


def apply_status(embed: discord.Embed, status: str) -> discord.Embed:
    """Return a copy of an existing triage embed with its Status field/colour updated."""
    new = embed.copy()
    if status in _GREEN_STATUSES:
        new.color = discord.Color.green()
    elif status in _GREY_STATUSES:
        new.color = discord.Color.dark_grey()
    for index, existing in enumerate(new.fields):
        if existing.name == "Status":
            new.set_field_at(
                index,
                name="Status",
                value=STATUS_LABELS.get(status, status),
                inline=True,
            )
            break
    return new


class BugTriageView(discord.ui.View):
    """Persistent triage actions. Interactions are routed via the cog's on_interaction
    handler keyed on the custom_id, so this view survives bot restarts."""

    def __init__(self, thread_id: int, *, active: bool = True):
        super().__init__(timeout=None)
        if not active:
            return
        self.add_item(
            discord.ui.Button(
                label="Create issue",
                style=discord.ButtonStyle.success,
                custom_id=f"bug_issue:{thread_id}",
                emoji="🛠️",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Close as Duplicate",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bug_dup:{thread_id}",
                emoji="🔁",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Close as Already Fixed",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bug_fixed:{thread_id}",
                emoji="✅",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Not a bug",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bug_notbug:{thread_id}",
                emoji="🚫",
            )
        )
