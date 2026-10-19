"""The weekly staff support report card (cogs/support_report.py)."""

import discord

from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.services.support_report import Insights, ReportData
from bulmaai.ui.v2 import card, facts, fit, head, trim

TITLE = "## 📊 Weekly support report"
FOOTER = "-# AI-written sections can be wrong. Nothing here goes live on its own."
SPEND_NOTE = "-# Spend covers support and crash-log AI replies (not ticket summaries, translation or vision)."


def _money(amount: float) -> str:
    return f"${amount:.2f}"


def _spend(data: ReportData) -> str:
    split = ", ".join(f"{item.model} {_money(item.cost)}" for item in data.usage if item.cost)
    return _money(data.spend) + (f" ({split})" if split else "")


def support_report_card(data: ReportData, insights: Insights | None) -> discord.ui.DesignerView:
    last_day = data.until - (data.until - data.since) / 7
    title = f"{TITLE}\n-# {data.since:%d %b} to {last_day:%d %b %Y} (UTC)"
    numbers = [
        facts(
            ("Tickets closed", data.tickets_closed),
            ("Solved", data.tickets_resolved),
            ("Unresolved", data.tickets_unresolved),
        ),
        facts(("AI replies", data.ai_replies), ("Escalations", data.escalations), ("Handoffs", data.handoffs), ("Crash-log diagnoses", data.log_diagnoses)),
        facts(("Support AI spend (est.)", _spend(data))),
    ]
    if data.ratings:  # 👍/👎 under the week's answers; no line without any
        rated = f"👍 {data.helpful} · 👎 {data.not_helpful} ({data.helpful_percent}% helpful)"
        numbers.insert(2, facts(("Answer feedback", rated)))
    if data.top_tags:
        numbers.append("**Top tags** " + ", ".join(f"{defuse_mentions(tag)} ({count})" for tag, count in data.top_tags))
    parts = ["\n".join(numbers), SPEND_NOTE]
    if insights is not None:
        if insights.themes:
            parts.append("**Recurring themes**\n" + "\n".join(f"- {trim(theme, 140)}" for theme in insights.themes))
        if insights.gaps:
            lines = (
                f"- {trim(question, 200)} → {trim(page, 80)}" if page else f"- {trim(question, 200)}"
                for question, page in insights.gaps
            )
            parts.append("**Questions the AI couldn't answer**\n" + "\n".join(lines))
        if insights.notes:
            parts.append("**Notes**\n" + trim(insights.notes, 500))
    else:
        parts.append("-# No AI summary this time.")
    parts.append(FOOTER)
    title, *body = fit([title, *parts])
    return card(head(title), *body)
