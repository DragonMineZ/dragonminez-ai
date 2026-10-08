"""Components V2 cards: the JSON the panel's block editor edits <-> a real Discord message.

A card is {"accent_color": "#RRGGBB" | None, "blocks": [...]} and renders as one Container.
normalize_card validates and cleans it, build_card_view renders it, card_from_message goes back.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import discord

MAX_COMPONENTS = 40
MAX_TEXT_CHARS = 4000
MAX_GALLERY_IMAGES = 10
MAX_BUTTONS = 5
LANGUAGE_ROW_COMPONENTS = 4
HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})")
SPACINGS = {"small": discord.SeparatorSpacingSize.small, "large": discord.SeparatorSpacingSize.large}
LANGUAGE_BUTTONS = (("en", "English", "🇺🇸"), ("es", "Español", "🇪🇸"), ("pt", "Português", "🇧🇷"))
LANGUAGE_PREFIX = "tpl_lang"


class AnnouncementError(Exception):
    """A payload problem safe to show the staff member who submitted it."""


def _url(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnnouncementError(f"{label} needs a link.")
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(value) > 2048:
        raise AnnouncementError(f"{label} must be an http(s) link.")
    return value


def _body(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnnouncementError(f"{label} can't be empty.")
    return value.strip()


def _normalize_block(raw: Any, number: int) -> dict[str, Any]:
    where = f"Block {number}"
    if not isinstance(raw, dict):
        raise AnnouncementError(f"{where} must be an object.")
    kind = raw.get("type")
    if kind == "text":
        return {"type": "text", "text": _body(raw.get("text"), f"{where} text")}
    if kind == "section":
        return {
            "type": "section",
            "text": _body(raw.get("text"), f"{where} text"),
            "thumbnail_url": _url(raw.get("thumbnail_url"), f"{where} thumbnail"),
        }
    if kind == "separator":
        spacing = raw.get("spacing", "small")
        if spacing not in SPACINGS:
            raise AnnouncementError(f"{where} spacing must be small or large.")
        return {"type": "separator", "divider": bool(raw.get("divider", True)), "spacing": spacing}
    if kind == "gallery":
        images = raw.get("images")
        if not isinstance(images, list) or not 1 <= len(images) <= MAX_GALLERY_IMAGES:
            raise AnnouncementError(f"{where}: a gallery needs 1 to {MAX_GALLERY_IMAGES} images.")
        cleaned = []
        for index, image in enumerate(images, 1):
            if not isinstance(image, dict):
                raise AnnouncementError(f"{where} image {index} must be an object.")
            description = image.get("description")
            if description is not None and not isinstance(description, str):
                raise AnnouncementError(f"{where} image {index} description must be text.")
            description = (description or "").strip() or None
            if description and len(description) > 1024:
                raise AnnouncementError(f"{where} image {index} description is over 1024 characters.")
            cleaned.append({"url": _url(image.get("url"), f"{where} image {index}"), "description": description})
        return {"type": "gallery", "images": cleaned}
    if kind == "buttons":
        buttons = raw.get("buttons")
        if not isinstance(buttons, list) or not 1 <= len(buttons) <= MAX_BUTTONS:
            raise AnnouncementError(f"{where}: a button row needs 1 to {MAX_BUTTONS} buttons.")
        cleaned = []
        for index, button in enumerate(buttons, 1):
            if not isinstance(button, dict):
                raise AnnouncementError(f"{where} button {index} must be an object.")
            label = _body(button.get("label"), f"{where} button {index} label")
            if len(label) > 80:
                raise AnnouncementError(f"{where} button {index} label is {len(label)} characters; the limit is 80.")
            entry = {"label": label, "url": _url(button.get("url"), f"{where} button {index}")}
            emoji = button.get("emoji")
            if emoji is not None and not isinstance(emoji, str):
                raise AnnouncementError(f"{where} button {index} emoji must be text.")
            if emoji and emoji.strip():
                entry["emoji"] = emoji.strip()
            cleaned.append(entry)
        return {"type": "buttons", "buttons": cleaned}
    raise AnnouncementError(f"{where} has an unknown type.")


def count_components(card: dict[str, Any], *, language_row: bool = False) -> int:
    sizes = {"text": 1, "section": 3, "separator": 1, "gallery": 1}
    total = 1 + (LANGUAGE_ROW_COMPONENTS if language_row else 0)
    for block in card["blocks"]:
        total += 1 + len(block["buttons"]) if block["type"] == "buttons" else sizes[block["type"]]
    return total


def count_chars(card: dict[str, Any]) -> int:
    total = 0
    for block in card["blocks"]:
        if block["type"] in ("text", "section"):
            total += len(block["text"])
        elif block["type"] == "gallery":
            total += sum(len(image["description"] or "") for image in block["images"])
    return total


def normalize_card(raw: Any, *, language_row: bool = False) -> dict[str, Any]:
    """Validates a card and returns the cleaned JSON. language_row reserves room for the language buttons."""
    if not isinstance(raw, dict):
        raise AnnouncementError("The message is missing its card.")
    color = raw.get("accent_color")
    if color in (None, ""):
        accent = None
    else:
        match = isinstance(color, str) and HEX_COLOR.fullmatch(color.strip())
        if not match:
            raise AnnouncementError("The accent color must be a hex code like #F39C12.")
        accent = f"#{match[1].upper()}"
    blocks = raw.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise AnnouncementError("Add at least one block.")
    card = {"accent_color": accent, "blocks": [_normalize_block(block, number) for number, block in enumerate(blocks, 1)]}
    components = count_components(card, language_row=language_row)
    if components > MAX_COMPONENTS:
        raise AnnouncementError(f"That's {components} components; Discord allows {MAX_COMPONENTS} per message.")
    chars = count_chars(card)
    if chars > MAX_TEXT_CHARS:
        raise AnnouncementError(f"The text totals {chars} characters; Discord allows {MAX_TEXT_CHARS} per message.")
    return card


def language_row(template_id: str) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(
        *(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.primary if code == "en" else discord.ButtonStyle.secondary,
                custom_id=f"{LANGUAGE_PREFIX}:{template_id}:{code}",
            )
            for code, label, emoji in LANGUAGE_BUTTONS
        )
    )


def build_card_view(card: dict[str, Any], *, extra_rows: tuple[discord.ui.ActionRow, ...] | list = ()) -> discord.ui.DesignerView:
    """Renders a normalized card; extra_rows (e.g. the language buttons) go at the bottom of the container."""
    items: list[discord.ui.Item] = []
    for block in card["blocks"]:
        kind = block["type"]
        if kind == "text":
            items.append(discord.ui.TextDisplay(block["text"]))
        elif kind == "section":
            items.append(
                discord.ui.Section(discord.ui.TextDisplay(block["text"]), accessory=discord.ui.Thumbnail(block["thumbnail_url"]))
            )
        elif kind == "separator":
            items.append(discord.ui.Separator(divider=block["divider"], spacing=SPACINGS[block["spacing"]]))
        elif kind == "gallery":
            items.append(
                discord.ui.MediaGallery(
                    *(discord.MediaGalleryItem(image["url"], description=image["description"]) for image in block["images"])
                )
            )
        else:
            items.append(
                discord.ui.ActionRow(
                    *(
                        discord.ui.Button(
                            style=discord.ButtonStyle.link, label=b["label"], url=b["url"], emoji=b.get("emoji") or None
                        )
                        for b in block["buttons"]
                    )
                )
            )
    accent = card["accent_color"]
    color = int(accent.lstrip("#"), 16) if accent else None
    return discord.ui.DesignerView(discord.ui.Container(*items, *extra_rows, color=color), timeout=None)


# ---------- reading a sent message back ----------


def _emoji_text(emoji: dict[str, Any] | None) -> str | None:
    if not emoji:
        return None
    if emoji.get("id"):
        return f"<{'a' if emoji.get('animated') else ''}:{emoji.get('name')}:{emoji['id']}>"
    return emoji.get("name")


def language_template_id(components: list[dict[str, Any]]) -> str | None:
    """The template id behind a message's language buttons, if it has them."""
    for container in components:
        for part in container.get("components", []):
            for child in part.get("components", []) if part.get("type") == 1 else []:
                custom_id = child.get("custom_id") or ""
                if custom_id.startswith(f"{LANGUAGE_PREFIX}:"):
                    return custom_id.split(":")[1]
    return None


def card_from_components(components: list[dict[str, Any]]) -> dict[str, Any]:
    """Maps component dicts of a V2 message back to card JSON; raises when the editor can't represent them."""
    containers = [c for c in components if c.get("type") == 17]
    if len(containers) != 1 or len(components) != 1:
        raise AnnouncementError("This isn't a single-card message, so the editor can't open it.")
    container = containers[0]
    blocks: list[dict[str, Any]] = []
    for part in container.get("components", []):
        kind = part.get("type")
        if kind == 10:
            blocks.append({"type": "text", "text": part["content"]})
        elif kind == 9:
            texts = part.get("components", [])
            accessory = part.get("accessory") or {}
            if len(texts) != 1 or accessory.get("type") != 11:
                raise AnnouncementError("This message has a section the editor can't open.")
            blocks.append({"type": "section", "text": texts[0]["content"], "thumbnail_url": accessory["media"]["url"]})
        elif kind == 14:
            spacing = "large" if part.get("spacing") == 2 else "small"
            blocks.append({"type": "separator", "divider": bool(part.get("divider", True)), "spacing": spacing})
        elif kind == 12:
            images = [
                {"url": item["media"]["url"], "description": item.get("description") or None} for item in part["items"]
            ]
            blocks.append({"type": "gallery", "images": images})
        elif kind == 1:
            children = part.get("components", [])
            if any((child.get("custom_id") or "").startswith(f"{LANGUAGE_PREFIX}:") for child in children):
                continue
            if not children or any(child.get("type") != 2 or child.get("style") != 5 for child in children):
                raise AnnouncementError("This message has buttons the editor can't open (only link buttons).")
            buttons = []
            for child in children:
                entry = {"label": child.get("label") or "", "url": child["url"]}
                if emoji := _emoji_text(child.get("emoji")):
                    entry["emoji"] = emoji
                buttons.append(entry)
            blocks.append({"type": "buttons", "buttons": buttons})
        else:
            raise AnnouncementError("This message has a block type the editor can't open.")
    accent = container.get("accent_color")
    card = {"accent_color": f"#{accent:06X}" if accent is not None else None, "blocks": blocks}
    return normalize_card(card, language_row=language_template_id(components) is not None)


def card_from_message(message: discord.Message) -> dict[str, Any]:
    """Raises AnnouncementError when the message isn't a V2 card the editor understands."""
    if not message.components or not any(getattr(c, "type", None) == discord.ComponentType.container for c in message.components):
        raise AnnouncementError("That message isn't a card (Components V2) message.")
    return card_from_components([component.to_dict() for component in message.components])
