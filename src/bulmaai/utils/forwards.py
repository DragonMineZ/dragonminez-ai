"""A forwarded message keeps its text and files in `message.snapshots`, not in `content` / `attachments`."""

from __future__ import annotations

from typing import Any


def _snapshots(message: Any) -> list[Any]:
    return [s.message for s in getattr(message, "snapshots", None) or () if getattr(s, "message", None)]


def forwarded_content(message: Any) -> str:
    return "\n".join(item.content for item in _snapshots(message) if item.content).strip()


def full_content(message: Any) -> str:
    """The message's own text plus the text of anything it forwards."""
    return "\n".join(p for p in (getattr(message, "content", "") or "", forwarded_content(message)) if p).strip()


def full_attachments(message: Any) -> list[Any]:
    """The message's own files plus the files of anything it forwards."""
    return [*(getattr(message, "attachments", None) or ()), *(a for item in _snapshots(message) for a in item.attachments or ())]


def inline_forwards(message: Any) -> None:
    """For renderers that only read `content` / `attachments` (chat_exporter): folds the forwarded text in as a quote
    and the forwarded files in as the message's own. Mutates the message; use it on throwaway history copies."""
    forwarded = forwarded_content(message)
    files = full_attachments(message)
    if not forwarded and len(files) == len(getattr(message, "attachments", None) or ()):
        return
    quote = "\n".join(f"> {line}" for line in forwarded.splitlines())
    message.content = "\n".join(p for p in (getattr(message, "content", "") or "", "*Forwarded message:*", quote) if p)
    message.attachments = files
