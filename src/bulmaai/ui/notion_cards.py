"""Cards for the Notion Tracker forum feed (cogs/notion_feed): the item card each post starts with, and the short notes
posted under it. English (a staff forum). Notion-supplied text is defused; senders also pass AllowedMentions.none()."""

import discord

from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.services.notion_feed import P0
from bulmaai.ui.v2 import card, facts, fit, head, trim

GREY = 0x95A5A6
DARK_GREY = 0x4F545C
STATUS_COLORS = {
    "Inbox": GREY,
    "Backlog": GREY,
    "In progress": 0x3498DB,
    "In review": 0xE67E22,
    "Done": 0x2ECC71,
    "Won't do": DARK_GREY,
}
P0_COLOR = 0xE74C3C
COMMENT_LIMIT = 1500
FOOTER = "-# Notion Tracker"


def clean(text: object) -> str:
    """Notion text made inert: no mentions, no markdown of its own."""
    return discord.utils.escape_markdown(defuse_mentions(str(text or "")))


def _color(item: dict) -> int:
    if item.get("priority") == P0:
        return P0_COLOR
    return STATUS_COLORS.get(item.get("status"), DARK_GREY)


def item_card(item: dict) -> discord.ui.DesignerView:
    title = clean(item.get("title") or "Untitled").replace("[", "(").replace("]", ")")
    name = f"{item['key']} · {title}" if item.get("key") else title
    line = f"### [{name}]({item['url']})" if item.get("url") else f"### {name}"
    details = facts(
        ("Type", clean(item.get("type")) or None),
        ("Project", clean(item.get("project")) or None),
        ("Priority", clean(item.get("priority")) or None),
        ("Status", clean(item.get("status")) or None),
    )
    assignees = [clean(person) for person in item.get("assignees") or []]
    people = f"**Assignee{'s' if len(assignees) > 1 else ''}** {', '.join(assignees)}" if assignees else ""
    parts = fit([line, details, people])
    return card(head(parts[0]), *parts[1:], FOOTER, color=_color(item))


def note(text: str) -> discord.ui.DesignerView:
    return card(text)


def status_note(author: str, old: str | None, new: str | None) -> discord.ui.DesignerView:
    return note(f"-# 🔄 **{clean(author)}** moved it **{clean(old or 'None')} → {clean(new or 'None')}**")


def priority_note(author: str, old: str | None, new: str | None) -> discord.ui.DesignerView:
    return note(f"-# ⬆️ **{clean(author)}** set priority **{clean(old or 'None')} → {clean(new or 'None')}**")


def deleted_note(author: str) -> discord.ui.DesignerView:
    return note(f"-# 🗑️ Deleted in Notion by **{clean(author)}**")


def comment_note(author: str, text: str) -> discord.ui.DesignerView:
    return note(f"💬 **{clean(author)}**: {trim(defuse_mentions(text), COMMENT_LIMIT)}")
