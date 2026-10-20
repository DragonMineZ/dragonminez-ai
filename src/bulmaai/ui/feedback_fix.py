"""The weekly "Answers to fix" review of 👎 answers: posted right after the support report (cogs/support_report.py),
handled in cogs/support_info.py. Staff-facing, English.

A header card, then one card per answer (up to 10): where and when, the requester's question and Bulma's reply as
inert excerpts, "✏️ Write the right answer" and "Skip". One card per answer, so each collapses on its own and no message
gets near the 40-component / 4000-character limits. Everything is routed by custom id, so a restart loses nothing:
fbfix:{write|skip}:{answer message id} on the buttons, fbfix_modal:{answer message id} on the form; the claim lives in
the ai_feedback row (reviewed_at)."""

from dataclasses import dataclass
from typing import Any

import discord

from bulmaai.services.ai_feedback import AnswerToFix
from bulmaai.services.ai_guard import defang, defuse_mentions
from bulmaai.services.support_info import BODY_MAX, KIND_PERMANENT, KIND_TEMPORARY, TITLE_MAX
from bulmaai.ui.v2 import card, facts, head, trim

PREFIX = "fbfix:"
MODAL_PREFIX = "fbfix_modal:"
WRITE, SKIP = "write", "skip"
QUESTION_EXCERPT = 300
REPLY_EXCERPT = 500
HEADER = "### 👎 Answers to fix"
COLOR = discord.Color.orange()
DONE_COLOR = discord.Color.dark_grey()
FIELD_TITLE, FIELD_ANSWER, FIELD_KIND, FIELD_DAYS = "title", "answer", "kind", "days"


def custom_id(action: str, message_id: int) -> str:
    return f"{PREFIX}{action}:{message_id}"


def parse_custom_id(value: str) -> tuple[str, int] | None:
    """(write or skip, answer message id) for a review card's button."""
    if not value.startswith(PREFIX):
        return None
    action, _, raw_id = value.removeprefix(PREFIX).partition(":")
    return (action, int(raw_id)) if action in (WRITE, SKIP) and raw_id.isdigit() else None


def modal_id(message_id: int) -> str:
    return f"{MODAL_PREFIX}{message_id}"


def parse_modal_id(value: str) -> int | None:
    raw_id = value.removeprefix(MODAL_PREFIX) if value.startswith(MODAL_PREFIX) else ""
    return int(raw_id) if raw_id.isdigit() else None


def excerpt(text: str, limit: int) -> str:
    """Member or model text on a staff card: one line, no pings, no clickable links or link previews, and markdown
    shown as typed."""
    flat = trim(" ".join(defang(str(text or "")).split()), limit)
    escaped = discord.utils.escape_markdown(flat)
    return "\\" + escaped if escaped.startswith(("#", "-#")) else escaped


def header_card(count: int) -> discord.ui.DesignerView:
    noun = "answer" if count == 1 else "answers"
    return card(
        head(
            f"{HEADER}\n-# {count} {noun} rated 👎 this week. Write the right answer and it goes live as support info "
            "for the AI, or skip it."
        ),
        color=COLOR,
    )


def item_card(item: AnswerToFix, where: str) -> discord.ui.DesignerView:
    """One 👎 answer: where ("<#channel>" or "DM"), language and day, the question and the reply, and the buttons."""
    meta = facts(("Where", where), ("Language", item.language), ("Sent", f"<t:{int(item.created_at.timestamp())}:d>"))
    buttons = [
        discord.ui.Button(
            label="Write the right answer",
            emoji="✏️",
            style=discord.ButtonStyle.primary,
            custom_id=custom_id(WRITE, item.message_id),
        ),
        discord.ui.Button(label="Skip", style=discord.ButtonStyle.secondary, custom_id=custom_id(SKIP, item.message_id)),
    ]
    return card(
        meta,
        f"**Question**\n> {excerpt(item.question, QUESTION_EXCERPT) or '(no text)'}",
        f"**Bulma's answer**\n> {excerpt(item.reply, REPLY_EXCERPT) or '(no text)'}",
        buttons=buttons,
        color=COLOR,
    )


def fixed_card(staff_id: int, info_id: int, title: str) -> discord.ui.DesignerView:
    """The item once someone wrote the right answer."""
    return card(f"✅ <@{staff_id}> → support info #{info_id}: {defuse_mentions(trim(title, TITLE_MAX))}", color=DONE_COLOR)


def skipped_card(staff_id: int) -> discord.ui.DesignerView:
    return card(f"⏭️ Skipped by <@{staff_id}>", color=DONE_COLOR)


def handled_card(staff_id: int | None, info_id: int | None, title: str = "") -> discord.ui.DesignerView:
    """The collapsed item for an answer already reviewed: fixed when it has a support info, else skipped."""
    staff_id = staff_id or 0
    return fixed_card(staff_id, info_id, title) if info_id else skipped_card(staff_id)


def fix_modal(message_id: int, question: str = "") -> discord.ui.DesignerModal:
    """The Write form: title (pre-filled from the question), the right answer, kind (permanent by default) and, for
    temporary entries, an optional expiry in days. Routed by custom id (store=False)."""
    title = trim(" ".join(defuse_mentions(question).split()), TITLE_MAX)
    title_input = discord.ui.InputText(
        style=discord.InputTextStyle.short,
        custom_id=FIELD_TITLE,
        max_length=TITLE_MAX,
        value=title or None,
        required=True,
    )
    answer_input = discord.ui.InputText(
        style=discord.InputTextStyle.long, custom_id=FIELD_ANSWER, max_length=BODY_MAX, required=True
    )
    kind_select = discord.ui.Select(
        custom_id=FIELD_KIND,
        options=[
            discord.SelectOption(label="Permanent", value=KIND_PERMANENT, default=True),
            discord.SelectOption(label="Temporary", value=KIND_TEMPORARY),
        ],
        required=True,
    )
    days_input = discord.ui.InputText(
        style=discord.InputTextStyle.short, custom_id=FIELD_DAYS, max_length=3, required=False
    )
    return discord.ui.DesignerModal(
        discord.ui.Label("Title", title_input),
        discord.ui.Label("Right answer", answer_input, description="Goes live as support info for the AI at once."),
        discord.ui.Label("Kind", kind_select),
        discord.ui.Label("Expires in days", days_input, description="Temporary only. Empty: no expiry."),
        title="Write the right answer",
        custom_id=modal_id(message_id),
        store=False,
    )


@dataclass(frozen=True)
class FixForm:
    title: str
    answer: str
    kind: str
    days: str


def _submitted(components: Any) -> dict[str, Any]:
    """custom id -> submitted value(s) of a modal_submit payload (Label components wrap their item in "component")."""
    out: dict[str, Any] = {}

    def walk(items: Any) -> None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("custom_id"):
                out[item["custom_id"]] = item["values"] if "values" in item else item.get("value")
            walk([item["component"]] if isinstance(item.get("component"), dict) else [])
            walk(item.get("components"))

    walk(components)
    return out


def read_form(components: Any) -> FixForm:
    """The Write form's values; the kind falls back to permanent when Discord sends none."""
    values = _submitted(components)
    kinds = values.get(FIELD_KIND)
    kind = kinds[0] if isinstance(kinds, list) and kinds else KIND_PERMANENT
    return FixForm(
        title=str(values.get(FIELD_TITLE) or ""),
        answer=str(values.get(FIELD_ANSWER) or ""),
        kind=str(kind),
        days=str(values.get(FIELD_DAYS) or "").strip(),
    )
