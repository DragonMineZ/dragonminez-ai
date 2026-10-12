"""Small building blocks for Components V2 cards: a header with an optional thumbnail, text that fits the
message's 4000-character budget, and button rows. Each feature lays out its own card with these."""

import logging

import discord

log = logging.getLogger(__name__)

TEXT_LIMIT = 3900  # a V2 message holds 4000 characters of text in total; leave a little room
WORKING = "⏳ Working…"
ALREADY_WORKING = "Already working on it."


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


def _action_rows(items):
    for item in items:
        if isinstance(item, discord.ui.ActionRow):
            yield item
        else:
            yield from _action_rows(getattr(item, "items", None) or [])


def busy(view: discord.ui.DesignerView, custom_id: str, label: str = WORKING) -> discord.ui.DesignerView:
    """view with every button in custom_id's row disabled and that button reading label; link buttons stay as they are."""
    for row in _action_rows(view.children):
        clickable = [item for item in row.children if isinstance(item, discord.ui.Button) and item.custom_id]
        if any(item.custom_id == custom_id for item in clickable):
            for item in clickable:
                item.disabled = True
                if item.custom_id == custom_id:
                    item.label, item.emoji = label, None
    return view


async def while_busy(interaction: discord.Interaction, working: set[int], work, *, failed: str) -> None:
    """Runs work() while the clicked card shows the button working with its row disabled; work leaves the card in its
    final state. Another click on the same message meanwhile only gets ALREADY_WORKING. If work raises or returns False,
    the card comes back as it was and the clicker gets failed (ephemeral)."""
    message = interaction.message
    if message.id in working:
        return await interaction.response.send_message(ALREADY_WORKING, ephemeral=True)
    working.add(message.id)
    try:
        custom_id = (interaction.data or {}).get("custom_id")
        await interaction.response.edit_message(view=busy(discord.ui.DesignerView.from_message(message, timeout=None), custom_id))
        try:
            ok = await work() is not False
        except Exception:
            log.exception("Button action failed", extra={"message_id": message.id, "custom_id": custom_id})
            ok = False
        if not ok:
            try:
                await interaction.edit_original_response(view=discord.ui.DesignerView.from_message(message, timeout=None))
            except discord.HTTPException:
                pass
            await interaction.followup.send(failed, ephemeral=True)
    finally:
        working.discard(message.id)


async def unbusy(interaction: discord.Interaction) -> None:
    """Inside while_busy work that already told the clicker why nothing happened: puts the card back as it was."""
    await interaction.edit_original_response(view=discord.ui.DesignerView.from_message(interaction.message, timeout=None))
