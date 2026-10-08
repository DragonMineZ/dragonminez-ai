"""The bug report triage card (Components V2). Buttons carry the thread id in their custom id and are routed by
the cog's on_interaction, so they survive restarts. Fixed component ids let a status change swap one line."""

import re

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
    # but read correctly on the card.
    "duplicate": "🔁 Closed as duplicate",
    "fixed": "✅ Already fixed",
}

# Display statuses that recolour the card like a "closed" outcome.
_GREEN_STATUSES = frozenset({"resolved", "fixed"})
_GREY_STATUSES = frozenset({"dismissed", "duplicate"})

STATUS_ID = 705
ACTIONS_ID = 800
DUPLICATE_TITLE = "🔁 Possible duplicate"
FIXED_TITLE = "✅ Possibly already fixed"


def _status_color(status: str, default: discord.Color) -> discord.Color:
    if status in _GREEN_STATUSES:
        return discord.Color.green()
    if status in _GREY_STATUSES:
        return discord.Color.dark_grey()
    return default


def _duplicate_line(duplicate: DuplicateAssessment) -> tuple[str, str] | None:
    """An AI duplicate/already-fixed suggestion as a (title, text) pair."""
    if not duplicate.has_match:
        return None
    ref = f"#{duplicate.issue_number} — {duplicate.issue_title}".strip()[:200]
    reason = f"\n{duplicate.reason}" if duplicate.reason else ""
    suffix = f" · confidence: {duplicate.confidence}"
    if duplicate.match_type == "duplicate":
        return DUPLICATE_TITLE, f"{ref}{suffix}{reason}"[:1000]
    return FIXED_TITLE, f"Closed issue {ref}{suffix}{reason}"[:1000]


def status_line(status: str, reporter_id: int | None) -> str:
    line = f"**Status** {STATUS_LABELS.get(status, status)}"
    return line + (f"　**Reporter** <@{reporter_id}>" if reporter_id else "")


def triage_buttons(thread_id: int) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(
        discord.ui.Button(label="Create issue", style=discord.ButtonStyle.success, custom_id=f"bug_issue:{thread_id}", emoji="🛠️"),
        discord.ui.Button(label="Close as Duplicate", custom_id=f"bug_dup:{thread_id}", emoji="🔁"),
        discord.ui.Button(label="Close as Already Fixed", custom_id=f"bug_fixed:{thread_id}", emoji="✅"),
        discord.ui.Button(label="Not a bug", custom_id=f"bug_notbug:{thread_id}", emoji="🚫"),
        id=ACTIONS_ID,
    )


def triage_view(
    triage: BugTriage,
    *,
    thread_id: int,
    status: str = "triaged",
    reporter_id: int | None = None,
    duplicate: DuplicateAssessment | None = None,
) -> discord.ui.DesignerView:
    color = _status_color(status, SEVERITY_COLORS.get(triage.severity, discord.Color.gold()))
    items: list[discord.ui.Item] = [
        discord.ui.TextDisplay(f"## 🐛 {triage.title}\n{triage.summary or 'No summary available.'}"[:1500]),
        discord.ui.TextDisplay(
            f"**Severity** {triage.severity.title()}　**Area** {triage.affected_area or 'Unknown'}　"
            f"**Likely a bug?** {'Yes' if triage.is_bug else 'Unclear / probably not'}"
        ),
    ]
    if triage.steps:
        steps = "\n".join(f"{index}. {step}" for index, step in enumerate(triage.steps[:8], start=1))
        items.append(discord.ui.TextDisplay(f"**Steps to reproduce**\n{steps}"[:1000]))
    if duplicate is not None and (line := _duplicate_line(duplicate)) is not None:
        items += [discord.ui.Separator(), discord.ui.TextDisplay(f"**{line[0]}** {line[1]}")]
    items += [
        discord.ui.TextDisplay(status_line(status, reporter_id), id=STATUS_ID),
        discord.ui.TextDisplay("-# AI-generated triage · staff actions below"),
    ]
    actions = [triage_buttons(thread_id)] if status == "triaged" else []
    return discord.ui.DesignerView(discord.ui.Container(*items, color=color), *actions, timeout=None)


def _walk(item):
    yield item
    for child in getattr(item, "items", None) or getattr(item, "children", None) or []:
        yield from _walk(child)


def restatus(message: discord.Message, status: str) -> dict:
    """message.edit kwargs that show the new status and drop the buttons."""
    view = discord.ui.DesignerView.from_message(message, timeout=None)
    if view.get_item(ACTIONS_ID) is not None:
        view.remove_item(ACTIONS_ID)
    container = next(item for item in view.children if isinstance(item, discord.ui.Container))
    if (line := container.get_item(STATUS_ID)) is not None:
        reporter = re.search(r"<@(\d+)>", line.content)
        line.content = status_line(status, int(reporter.group(1)) if reporter else None)
    container.color = _status_color(status, container.color)
    return {"view": view}


def message_text(message: discord.Message) -> str:
    """The card's text, one "**Name** value" line per fact."""
    view = discord.ui.DesignerView.from_message(message, timeout=None)
    return "\n".join(item.content for item in _walk(view) if isinstance(item, discord.ui.TextDisplay))
