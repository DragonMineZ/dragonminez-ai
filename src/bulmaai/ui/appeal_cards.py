"""Staff cards for appeals from the appeals website (services/ban_appeals.py). Pure builders: the card is rebuilt
from the appeal row on every change. Buttons: banappeal:<accept|deny|ask>:<appeal_id>; their forms:
banappeal_form:<deny|ask>:<appeal_id>. Both are routed by custom id in cogs/ban_appeals.py."""

from collections import Counter
from typing import Any

import discord

from bulmaai.services.ai_guard import defang
from bulmaai.ui.mod_cards import quote_block
from bulmaai.ui.v2 import fit, trim

BUTTON = "banappeal"
FORM = "banappeal_form"
ACTIONS = ("accept", "deny", "ask")
FORMS = ("deny", "ask")
FIELD_TEXT = "text"
KIND_TITLES = {"ban": "🔨 Ban appeal", "timeout": "🔇 Timeout appeal"}


def custom_id(action: str, appeal_id: int) -> str:
    return f"{BUTTON}:{action}:{appeal_id}"


def form_id(action: str, appeal_id: int) -> str:
    return f"{FORM}:{action}:{appeal_id}"


def _parse(value: str | None, prefix: str, allowed: tuple[str, ...]) -> tuple[str, int] | None:
    parts = (value or "").split(":")
    if len(parts) != 3 or parts[0] != prefix or parts[1] not in allowed or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2])


def parse_custom_id(value: str | None) -> tuple[str, int] | None:
    return _parse(value, BUTTON, ACTIONS)


def parse_form_id(value: str | None) -> tuple[str, int] | None:
    return _parse(value, FORM, FORMS)


def _ts(when, style: str = "R") -> str:
    return discord.utils.format_dt(when, style) if when else "unknown"


def _history(cases) -> str:
    if not cases:
        return "**Case history** none"
    counts = Counter(case.action for case in cases)
    total = f"{len(cases)}+" if len(cases) >= 25 else str(len(cases))
    summary = ", ".join(f"{count} {action}" for action, count in counts.most_common())
    recent = [
        f"-# #{case.id} {case.action} {_ts(case.created_at, 'd')}"
        + (f": {trim(discord.utils.escape_markdown(defang(case.reason)), 60)}" if case.reason else "")
        for case in cases[:5]
    ]
    return f"**Case history** {total} ({summary})\n" + "\n".join(recent)


def outcome_line(appeal: Any) -> str | None:
    if appeal.outcome == "accepted":
        lifted = "unbanned" if appeal.kind == "ban" else "timeout removed"
        return f"✅ Accepted ({lifted}) by <@{appeal.outcome_by}> {_ts(appeal.outcome_at)}"
    if appeal.outcome == "denied":
        line = f"❌ Denied by <@{appeal.outcome_by}> {_ts(appeal.outcome_at)}"
        if appeal.staff_note:
            line += f"\n**Reason (shown to them)** {trim(discord.utils.escape_markdown(defang(appeal.staff_note)), 500)}"
        return line
    return None


def _head(appeal: Any) -> str:
    name = discord.utils.escape_markdown(defang(appeal.user_name or "unknown"))
    return f"### {KIND_TITLES.get(appeal.kind, 'Appeal')} #{appeal.id}\n<@{appeal.user_id}> **{name}** (`{appeal.user_id}`)"


def appeal_card(appeal: Any, cases=()) -> discord.ui.DesignerView:
    """Open: the whole appeal plus Accept / Deny / Ask a question. Decided: collapsed to who decided and how.
    Everything the member wrote is quoted and defanged (no pings, no clickable links)."""
    outcome = outcome_line(appeal)
    if outcome:
        color = discord.Color.green() if appeal.outcome == "accepted" else discord.Color.dark_grey()
        container = discord.ui.Container(
            discord.ui.TextDisplay(_head(appeal)), discord.ui.TextDisplay(outcome), color=color
        )
        return discord.ui.DesignerView(container, timeout=None)

    reason = trim(discord.utils.escape_markdown(defang(appeal.ban_reason or "No reason given")), 500)
    when = (
        f"**Banned** {_ts(appeal.punished_at)}"
        if appeal.kind == "ban"
        else f"**Timed out until** {_ts(appeal.ends_at, 'f')}"
    )
    parts = [
        f"**Reason** {reason}\n{when}　**Sent** {_ts(appeal.created_at)}",
        _history(cases),
        "**Why it should be lifted**\n" + (quote_block(appeal.answers.get("why"), 1500) or "> *empty*"),
    ]
    if extra := quote_block(appeal.answers.get("extra"), 500):
        parts.append("**Anything else**\n" + extra)
    if appeal.question:
        qa = f"**❓ Question from <@{appeal.question_by}>** {_ts(appeal.question_asked_at)}\n" + (
            quote_block(appeal.question, 500) or ""
        )
        if appeal.user_reply:
            qa += f"\n**Their answer** {_ts(appeal.reply_at)}\n" + (quote_block(appeal.user_reply, 1000) or "")
        else:
            qa += "\n-# Waiting for their answer on the appeals site."
        parts.append(qa)
    parts = fit([_head(appeal), *parts])
    buttons = [
        discord.ui.Button(label="Accept", style=discord.ButtonStyle.success, custom_id=custom_id("accept", appeal.id)),
        discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=custom_id("deny", appeal.id)),
        discord.ui.Button(label="Ask a question", style=discord.ButtonStyle.secondary, custom_id=custom_id("ask", appeal.id)),
    ]
    items = [discord.ui.TextDisplay(parts[0]), discord.ui.Separator()]
    items += [discord.ui.TextDisplay(part) for part in parts[1:]]
    container = discord.ui.Container(*items, discord.ui.ActionRow(*buttons), color=discord.Color.blurple())
    return discord.ui.DesignerView(container, timeout=None)


def reply_card(appeal: Any) -> discord.ui.DesignerView:
    text = f"💬 <@{appeal.user_id}> answered on the appeals site:\n" + (quote_block(appeal.user_reply, 1000) or "")
    return discord.ui.DesignerView(discord.ui.Container(discord.ui.TextDisplay(text), color=discord.Color.blurple()), timeout=None)


def _modal(action: str, appeal_id: int, *, title: str, label: str, description: str, required: bool, max_length: int):
    text = discord.ui.InputText(
        style=discord.InputTextStyle.long, custom_id=FIELD_TEXT, required=required, max_length=max_length
    )
    return discord.ui.DesignerModal(
        discord.ui.Label(label, text, description=description),
        title=title,
        custom_id=form_id(action, appeal_id),
        store=False,
    )


def deny_modal(appeal_id: int) -> discord.ui.DesignerModal:
    return _modal(
        "deny",
        appeal_id,
        title="Deny appeal",
        label="Reason (optional)",
        description="Shown to them on the appeals site.",
        required=False,
        max_length=500,
    )


def ask_modal(appeal_id: int) -> discord.ui.DesignerModal:
    return _modal(
        "ask",
        appeal_id,
        title="Ask a question",
        label="Question",
        description="They see it on the appeals site and answer there.",
        required=True,
        max_length=500,
    )


def read_text(components: Any) -> str:
    """The modal's one text field, from a modal_submit payload (Label components wrap their item in "component")."""
    stack = list(components) if isinstance(components, list) else []
    while stack:
        item = stack.pop(0)
        if not isinstance(item, dict):
            continue
        if item.get("custom_id") == FIELD_TEXT:
            return str(item.get("value") or "").strip()
        if isinstance(item.get("component"), dict):
            stack.append(item["component"])
        stack.extend(item.get("components") or [])
    return ""
