"""Small building blocks for Components V2 cards: a header with an optional thumbnail, text that fits the
message's 4000-character budget, and button rows. Each feature lays out its own card with these."""

import discord

TEXT_LIMIT = 3900  # a V2 message holds 4000 characters of text in total; leave a little room


def trim(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def fit(parts: list[str], budget: int = TEXT_LIMIT) -> list[str]:
    """Trims the longest parts until all of them together fit budget; short parts stay whole."""
    parts = [part for part in parts if part]
    while (over := sum(map(len, parts)) - budget) > 0:
        longest = max(range(len(parts)), key=lambda i: len(parts[i]))
        if len(parts[longest]) <= 60:
            break  # ponytail: only hit with dozens of parts; callers keep their part count small
        parts[longest] = trim(parts[longest], max(len(parts[longest]) - over, 60))
    return parts


def head(text: str, thumbnail: str | None = None) -> discord.ui.Item:
    """The card's top block, with the thumbnail beside it when there is one."""
    display = discord.ui.TextDisplay(text)
    return discord.ui.Section(display, accessory=discord.ui.Thumbnail(thumbnail)) if thumbnail else display


def facts(*pairs: tuple[str, object]) -> str:
    """'**Name** value　**Name** value', skipping empty values."""
    return "　".join(f"**{name}** {value}" for name, value in pairs if value not in (None, ""))


def rows(items) -> list[discord.ui.ActionRow]:
    """Buttons five to a row; a select gets a row of its own."""
    out: list[discord.ui.ActionRow] = []
    row: list[discord.ui.Item] = []
    for item in items or []:
        if isinstance(item, discord.ui.Select):
            out.append(discord.ui.ActionRow(item))
            continue
        row.append(item)
        if len(row) == 5:
            out.append(discord.ui.ActionRow(*row))
            row = []
    if row:
        out.append(discord.ui.ActionRow(*row))
    return out


def card(*items, color: discord.Colour | int | None = None, buttons=None) -> discord.ui.DesignerView:
    """One container: strings become text blocks, None is skipped, components pass through; buttons go last."""
    parts = [discord.ui.TextDisplay(item) if isinstance(item, str) else item for item in items if item]
    return discord.ui.DesignerView(discord.ui.Container(*parts, *rows(buttons), color=color), timeout=None)
