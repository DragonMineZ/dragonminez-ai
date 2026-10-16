"""Cards for staff support info (cogs/support_info.py): the daily public known-issues post, the /info list, and the
staff review card for AI suggestions (and its handled form)."""

from collections.abc import Sequence

import discord

from bulmaai.services.support_info import SupportInfo
from bulmaai.ui.v2 import TEXT_LIMIT, card, facts, fit, head, trim

DAILY_HEAD = "## 📌 Current known issues | Problemas conocidos actuales"
DAILY_FOOTER = "-# Updated every 24 hours | Actualizado cada 24 horas"


def _stamp(when) -> str:
    return f"<t:{int(when.timestamp())}:R>"


def entry_block(entry: SupportInfo) -> str:
    since = f"-# Since {_stamp(entry.created_at)}"
    if entry.expires_at is not None:
        since += f" · until {_stamp(entry.expires_at)}"
    return f"**{entry.title}**\n{entry.body}\n{since}"


def daily_post_card(entries: Sequence[SupportInfo]) -> discord.ui.DesignerView:
    """entries newest first; the oldest are left out whole when they don't all fit in one message."""
    budget = TEXT_LIMIT - len(DAILY_HEAD) - len(DAILY_FOOTER)
    blocks: list[str] = []
    for block in map(entry_block, entries):
        if sum(map(len, blocks)) + len(block) > budget:
            break
        blocks.append(block)
    return card(head(DAILY_HEAD), *blocks, DAILY_FOOTER)


def list_line(entry: SupportInfo) -> str:
    line = f"#{entry.id} · {entry.kind} · {trim(entry.title, 100)}"
    if entry.expires_at is not None:
        line += f" · until {_stamp(entry.expires_at)}"
    return line


REVIEW_HEAD = "### 💡 Suggested support info"
REVIEW_PREFIX = "supinfo"
APPROVE, EDIT, DISCARD = "approve", "edit", "discard"


def review_custom_id(action: str, info_id: int) -> str:
    return f"{REVIEW_PREFIX}:{action}:{info_id}"


def parse_review_custom_id(custom_id: str) -> tuple[str, int] | None:
    prefix, _, rest = custom_id.partition(":")
    action, _, raw_id = rest.partition(":")
    if prefix != REVIEW_PREFIX or action not in (APPROVE, EDIT, DISCARD) or not raw_id.isdigit():
        return None
    return action, int(raw_id)


def _ticket_ref(ticket_id: int | None, page_link: str | None) -> str | None:
    parts = ([f"#{ticket_id:04d}"] if ticket_id is not None else []) + ([f"[transcript]({page_link})"] if page_link else [])
    return " · ".join(parts) or None


def review_card(entry: SupportInfo, *, page_link: str | None = None, expiry_days: int = 7) -> discord.ui.DesignerView:
    """The staff review card for an AI suggestion; buttons are routed by custom id in SupportInfoCog.on_interaction."""
    temporary = entry.kind == "temporary"
    meta = facts(
        ("Kind", entry.kind),
        ("From ticket", _ticket_ref(entry.ticket_id, page_link)),
        ("Expires", f"{expiry_days} days after approval" if temporary else None),
    )
    title, body = fit([f"**{entry.title}**", entry.body], TEXT_LIMIT - len(REVIEW_HEAD) - len(meta))
    buttons = [
        discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=review_custom_id(APPROVE, entry.id)),
        discord.ui.Button(label="Edit & approve", style=discord.ButtonStyle.secondary, custom_id=review_custom_id(EDIT, entry.id)),
        discord.ui.Button(label="Discard", style=discord.ButtonStyle.danger, custom_id=review_custom_id(DISCARD, entry.id)),
    ]
    return card(head(REVIEW_HEAD), meta, title, body, buttons=buttons)


def handled_line(entry: SupportInfo) -> str:
    by = f"<@{entry.updated_by}>" if entry.updated_by else "staff"
    if entry.status == "active":
        return f"✅ Approved by {by} · support info #{entry.id}"
    if entry.status == "removed":
        return f"🗑️ Removed by {by}"
    return f"🗑️ Discarded by {by}"


def review_handled_card(entry: SupportInfo) -> discord.ui.DesignerView:
    """The review card once someone approved or discarded it: the suggestion stays (as approved, if edited), the
    buttons go and the outcome is added."""
    meta = facts(("Kind", entry.kind), ("From ticket", _ticket_ref(entry.ticket_id, None)))
    outcome = f"-# {handled_line(entry)}"
    title, body = fit([f"**{entry.title}**", entry.body], TEXT_LIMIT - len(REVIEW_HEAD) - len(meta) - len(outcome))
    return card(head(REVIEW_HEAD), meta, title, body, outcome, color=discord.Color.dark_grey())


def info_list_card(entries: Sequence[SupportInfo], pending: int = 0) -> discord.ui.DesignerView:
    lines = [list_line(entry) for entry in entries] or ["No active support info."]
    body = "\n".join(lines)
    footer = f"-# {pending} pending suggestion{'s' if pending != 1 else ''}" if pending else None
    return card(head("### Support info"), trim(body, TEXT_LIMIT - 100), footer)
