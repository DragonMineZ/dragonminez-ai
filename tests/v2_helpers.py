"""Reading Components V2 cards in tests."""

import re

import discord

_HEADER = re.compile(r"^\*\*(.+?)\*\*$")


def walk(item):
    yield item
    for child in getattr(item, "items", None) or getattr(item, "children", None) or []:
        yield from walk(child)
    if accessory := getattr(item, "accessory", None):
        yield accessory


def texts(view) -> list[str]:
    return [item.content for item in walk(view) if isinstance(item, discord.ui.TextDisplay)]


def text(view) -> str:
    return "\n".join(texts(view))


def buttons(view) -> list:
    return [item for item in walk(view) if isinstance(item, discord.ui.Button)]


def section(view, name_fragment: str) -> str | None:
    """The lines under a '**Name**' header line, up to the next header or the end of that text block."""
    for block in texts(view):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            if (match := _HEADER.match(line)) and name_fragment in match[1]:
                rest = []
                for following in lines[i + 1 :]:
                    if _HEADER.match(following):
                        break
                    rest.append(following)
                return "\n".join(rest)
    return None
