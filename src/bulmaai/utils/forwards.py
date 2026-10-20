"""A forwarded message keeps its text and files in `message.snapshots`, not in `content` / `attachments`. A V2 card
the bot sends with files keeps them in its File / Media Gallery components: Discord leaves `attachments` empty."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import discord


def _snapshots(message: Any) -> list[Any]:
    return [s.message for s in getattr(message, "snapshots", None) or () if getattr(s, "message", None)]


def forwarded_content(message: Any) -> str:
    return "\n".join(item.content for item in _snapshots(message) if item.content).strip()


def full_content(message: Any) -> str:
    """The message's own text plus the text of anything it forwards."""
    return "\n".join(p for p in (getattr(message, "content", "") or "", forwarded_content(message)) if p).strip()


def _component_files(message: Any) -> list[Any]:
    """The uploaded files a V2 card shows (File components, gallery items), as Attachments. External media is skipped."""
    state = getattr(message, "_state", None)
    out: list[Any] = []

    def add(media: Any, name: str | None = None, size: int | None = None) -> None:
        url = getattr(media, "url", None) or ""
        path = urlparse(url).path.split("/")
        # A cdn.discordapp.com/attachments/{channel}/{id}/{name} url says the id when the payload doesn't.
        attachment_id = getattr(media, "attachment_id", None) or (path[-2] if len(path) > 2 and path[-2].isdigit() else None)
        if state is None or attachment_id is None:
            return
        data = {
            "id": attachment_id, "filename": name or path[-1], "size": size or 0, "url": url,
            "proxy_url": getattr(media, "proxy_url", None), "content_type": getattr(media, "content_type", None),
            "height": getattr(media, "height", None), "width": getattr(media, "width", None),
        }
        out.append(discord.Attachment(data=data, state=state))

    def walk(items: Any) -> None:
        for item in items or ():
            if isinstance(item, discord.components.FileComponent):
                add(item.file, item.name, item.size)
            elif isinstance(item, discord.components.MediaGallery):
                for entry in item.items:
                    add(entry.media)
            for attr in ("components", "children"):
                if isinstance(nested := getattr(item, attr, None), list):
                    walk(nested)

    walk(getattr(message, "components", None))
    return out


def full_attachments(message: Any) -> list[Any]:
    """The message's own files (or, for a V2 card, the files its components show) plus the files of anything it
    forwards."""
    own = getattr(message, "attachments", None) or _component_files(message)
    return [*own, *(a for item in _snapshots(message) for a in item.attachments or ())]


def inline_forwards(message: Any) -> None:
    """For renderers that only read `content` / `attachments` (chat_exporter): folds the forwarded text in as a quote
    and the forwarded files in as the message's own. Mutates the message; use it on throwaway history copies."""
    forwarded = forwarded_content(message)
    files = full_attachments(message)
    if not forwarded and len(files) == len(getattr(message, "attachments", None) or ()):
        return
    if forwarded:
        quote = "\n".join(f"> {line}" for line in forwarded.splitlines())
        message.content = "\n".join(p for p in (getattr(message, "content", "") or "", "*Forwarded message:*", quote) if p)
    message.attachments = files
