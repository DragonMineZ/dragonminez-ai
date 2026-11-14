"""Cards for staff support info (cogs/support_info.py): the daily public known-issues post and the /info list."""

from collections.abc import Sequence

import discord

from bulmaai.services.support_info import SupportInfo
from bulmaai.ui.v2 import TEXT_LIMIT, card, head, trim

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


def info_list_card(entries: Sequence[SupportInfo], pending: int = 0) -> discord.ui.DesignerView:
    lines = [list_line(entry) for entry in entries] or ["No active support info."]
    body = "\n".join(lines)
    footer = f"-# {pending} pending suggestion{'s' if pending != 1 else ''}" if pending else None
    return card(head("### Support info"), trim(body, TEXT_LIMIT - 100), footer)
