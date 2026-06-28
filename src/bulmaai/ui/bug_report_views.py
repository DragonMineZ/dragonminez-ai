import discord

from bulmaai.services.bug_report_ai import BugTriage

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
}


def build_triage_embed(
    triage: BugTriage,
    *,
    status: str = "triaged",
    reporter_id: int | None = None,
) -> discord.Embed:
    color = SEVERITY_COLORS.get(triage.severity, discord.Color.gold())
    if status == "resolved":
        color = discord.Color.green()
    elif status == "dismissed":
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
    if reporter_id is not None:
        embed.add_field(name="Reporter", value=f"<@{reporter_id}>", inline=True)
    embed.add_field(name="Status", value=STATUS_LABELS.get(status, status), inline=True)
    embed.set_footer(text="AI-generated triage · staff actions below")
    return embed


def apply_status(embed: discord.Embed, status: str) -> discord.Embed:
    """Return a copy of an existing triage embed with its Status field/colour updated."""
    new = embed.copy()
    if status == "resolved":
        new.color = discord.Color.green()
    elif status == "dismissed":
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
                label="Not a bug",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bug_notbug:{thread_id}",
                emoji="🚫",
            )
        )
