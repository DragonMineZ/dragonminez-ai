"""Cards for the log corpus (cogs/log_corpus.py): the notice per collected log with its Exclude/Include toggle, the
threshold ping with Download .zip, and the posted zips."""

import discord

from bulmaai.services.ai_guard import defang
from bulmaai.ui.v2 import card, facts, trim

PREFIX = "logcorpus"
EXCLUDE, INCLUDE, ZIP = "exclude", "include", "zip"

NOTICE_HEAD = "## 📥 Possible log entry"
EXCLUDED_LINE = "-# 🚫 **Excluded**: left out of the .zip. **Include** puts it back."
NOT_ATTACHED_LINE = "-# The redacted log is too big to attach here; it is still in the .zip."
NOTICE_COLOR = discord.Colour.blurple()
EXCLUDED_COLOR = discord.Colour.dark_grey()
PING_COLOR = discord.Colour.gold()
DONE_COLOR = discord.Colour.green()


def toggle_custom_id(action: str, message_id: int, key: str) -> str:
    return f"{PREFIX}:{action}:{message_id}:{key}"


def zip_custom_id(version: str) -> str:
    return f"{PREFIX}:{ZIP}:{version}"


def parse_custom_id(custom_id: str) -> tuple[str, int | None, str] | None:
    """(action, message_id, key) for Exclude/Include, (ZIP, None, version) for Download .zip, else None."""
    prefix, _, rest = custom_id.partition(":")
    action, _, rest = rest.partition(":")
    if prefix != PREFIX or not rest:
        return None
    if action == ZIP:
        return action, None, rest
    raw_id, _, key = rest.partition(":")
    if action not in (EXCLUDE, INCLUDE) or not raw_id.isdigit() or not key:
        return None
    return action, int(raw_id), key


def _code(text: object) -> str:
    return "`" + defang(str(text)).replace("`", "'") + "`"


def cause_line(root_cause: dict | None) -> str | None:
    """The parser's verdict in one line: culprit mod/jar and the deepest exception, defanged."""
    if not root_cause or not root_cause.get("exception"):
        return None
    mod, jar = root_cause.get("culprit_mod"), root_cause.get("culprit_jar")
    exception = _code(trim(root_cause["exception"], 300))
    if not (mod or jar):
        return f"🎯 {exception}"
    where = f"{_code(mod)} ({_code(jar)})" if mod and jar else _code(mod or jar)
    lead = "Mixin injection failed in" if root_cause.get("kind") == "mixin" else "Points to"
    return f"🎯 {lead} {where}: {exception}"


def notice_card(
    context: dict, *, jump_url: str, message_id: int, key: str, attachment: str | None, excluded: bool
) -> discord.ui.DesignerView:
    """attachment is the uploaded redacted log's file name, None when it was too big to attach."""
    versions = facts(
        ("DMZ", _code(context.get("dmz_version") or "unknown")),
        ("Minecraft", context.get("minecraft_version") and _code(context["minecraft_version"])),
        ("Forge", context.get("forge_version") and _code(context["forge_version"])),
    )
    details = facts(("File", _code(context.get("file") or "?")), ("Errors", context.get("error_count", 0)))
    toggle = discord.ui.Button(
        label="Include" if excluded else "Exclude",
        emoji="↩️" if excluded else "🚫",
        style=discord.ButtonStyle.success if excluded else discord.ButtonStyle.secondary,
        custom_id=toggle_custom_id(INCLUDE if excluded else EXCLUDE, message_id, key),
    )
    return card(
        f"{NOTICE_HEAD}\n{versions}\n{details}",
        cause_line(context.get("root_cause")),
        discord.ui.File(f"attachment://{attachment}") if attachment else NOT_ATTACHED_LINE,
        EXCLUDED_LINE if excluded else None,
        color=EXCLUDED_COLOR if excluded else NOTICE_COLOR,
        buttons=[discord.ui.Button(label="Jump to message", url=jump_url), toggle],
    )


def ping_title(count: int, version: str) -> str:
    return f"**{count} logs collected for DragonMineZ {defang(version)}**"


def ping_card(count: int, version: str, user_id: int) -> discord.ui.DesignerView:
    return card(
        f"<@{user_id}> {ping_title(count, version)}\n"
        "-# Download them as a .zip. The bot deletes its copies once the .zip is posted.",
        color=PING_COLOR,
        buttons=[
            discord.ui.Button(
                label="Download .zip", emoji="📦", style=discord.ButtonStyle.primary, custom_id=zip_custom_id(version)
            )
        ],
    )


def ping_done_card(version: str, bundled: int) -> discord.ui.DesignerView:
    """The ping after a download: no button, and no mention in the text."""
    done = f"✅ {bundled} logs posted as a .zip." if bundled else "✅ Already downloaded."
    return card(f"**Logs collected for DragonMineZ {defang(version)}**\n-# {done}", color=DONE_COLOR)


def bundle_card(label: str, logs: int, part: int, parts: int, filename: str) -> discord.ui.DesignerView:
    line = facts(("Logs", logs), ("Part", f"{part}/{parts}" if parts > 1 else None))
    return card(
        f"### 📦 Log corpus: DragonMineZ {defang(label)}\n{line}",
        discord.ui.File(f"attachment://{filename}"),
        "-# Redacted. Keep it local, never commit it, delete it when the analyzer work is done.",
        color=DONE_COLOR,
    )
