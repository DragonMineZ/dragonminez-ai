"""Hosted log viewer: an analyzed log or /dmzdebug dump, redacted, rendered once to a self-contained HTML page and
served for 24 hours at <log_page_public_url>/l/<token>.

The token is the only credential (same 192-bit tokens as ticket transcripts). Expiry is the file's mtime, so it
survives restarts without a DB row; the log_parser cog's hourly sweep deletes old files.
"""

import asyncio
import html
import logging
import re
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bulmaai.services.ticket_pages import TOKEN_PATTERN, new_token
from bulmaai.utils.log_parser import is_noise

log = logging.getLogger(__name__)

EXPIRY_SECONDS = 24 * 3600
_TOKEN_RE = re.compile(TOKEN_PATTERN)

# Personal data a player doesn't mean to publish. Kept conservative: mod versions like 1.20.1.2 must survive.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # C:\Users\<name>\... and C:/Users/<name>/... (the Windows account name is often a real name)
    (re.compile(r"([A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)[^\\/\r\n\"'<>|]+", re.IGNORECASE), r"\1<user>"),
    (re.compile(r"(/(?:home|Users)/)[^/\s\"']+"), r"\1<user>"),
    # Launcher arguments that identify the account or the server it joins
    (re.compile(r"(--(?:username|uuid|xuid|clientId|accessToken|session|server|quickPlayMultiplayer)[,=\s]+)[^\s,\]]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(Connecting to )[^\s,]+"), r"\1<server>"),
    # IPs as Java prints them: "/1.2.3.4" or "1.2.3.4:25565"
    (re.compile(r"(?<=/)(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:\d{1,3}\.){3}\d{1,3}(?=:\d)"), "<ip>"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), "<email>"),
    # Session / access tokens: "token:eyJ...:uuid", "accessToken=...", "sessionId: ..."
    (re.compile(r"(token:)[^\s,\]]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"(access[_-]?token|session[_-]?id)(\"?\s*[:=]\s*\"?)[^\s,\"\]]+", re.IGNORECASE), r"\1\2<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"), "<redacted>"),
    # UUIDs that name a player; mod and registry UUIDs stay
    (re.compile(r"(UUID of player [^\n]*? is )[0-9a-fA-F-]{32,36}"), r"\1<uuid>"),
    (re.compile(r"(\b(?:player|user|profile|account)[ _-]?(?:uuid|id)\b[\s:=\"']+)[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE), r"\1<uuid>"),
)

# The account's own name, learned from the log itself and then scrubbed from every line (chat, paths, file names).
_PLAYER_NAME_RES = (
    re.compile(r"Setting user: (\w{3,16})\b"),
    re.compile(r"--username[,=\s]+(\w{3,16})\b", re.IGNORECASE),
    re.compile(r"\]: (\w{3,16}) (?:joined|left) the game"),
    re.compile(r"\]: (\w{3,16}) lost connection"),
    re.compile(r"\]: (\w{3,16})\[/[^\]\s]+\] logged in with entity id"),
)
_NOT_NAMES = frozenset({"Player", "Dev", "Server", "Client", "Minecraft", "null", "None"})


def player_names(text: str) -> set[str]:
    return {m.group(1) for pattern in _PLAYER_NAME_RES for m in pattern.finditer(text)} - _NOT_NAMES

_RE_LEVEL = re.compile(r"^\[[^\]\n]*\]\s*\[[^\]\n]*?/(FATAL|ERROR|WARN|INFO|DEBUG|TRACE)\]")
_RE_TRACE = re.compile(r"^\s+at |^\s*Caused by:|^\s*\.\.\. \d+ more|^\s*Suppressed:")
_RE_DEBUG_HEADER = re.compile(r"^#{1,2} ")
_RE_DEBUG_PAIR = re.compile(r"^(\s*(?:- )?)([\w .()\[\]:-]+?):( .*)?$")
_RE_JSON_PAIR = re.compile(r'^( *)"([^"\n]*)": (.*)$')
_LEVELS = ("ERROR", "WARN", "INFO", "DEBUG")


def redact(text: str, names: Iterable[str] = ()) -> str:
    """names: account names learned elsewhere (e.g. from the log a chat message sits next to), masked as well."""
    for name in sorted(player_names(text) | set(names), key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", "<player>", text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def page_url(settings: Any, token: str) -> str:
    return f"{settings.log_page_public_url.rstrip('/')}/l/{token}"


def page_path(settings: Any, token: str) -> Path:
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("Invalid log page token.")
    return Path(settings.log_page_dir) / f"{token}.html"


def _log_rows(lines: list[str]) -> list[str]:
    rows: list[str] = []
    level = "INFO"
    for number, line in enumerate(lines, 1):
        if match := _RE_LEVEL.match(line):
            level = "ERROR" if match.group(1) == "FATAL" else ("DEBUG" if match.group(1) == "TRACE" else match.group(1))
        # Frames and unprefixed lines belong to the entry above them, so filtering hides a whole stacktrace at once.
        css = f"l-{level}" + (" tr" if _RE_TRACE.match(line) else "")
        hint = ""
        if level in ("ERROR", "WARN") and is_noise(line):
            css, hint = css + " noise", ' title="Known harmless message"'
        rows.append(
            f'<div class="r {css}" id="L{number}"{hint}><a href="#L{number}">{number}</a><span>{html.escape(line)}</span></div>'
        )
    return rows


def _debug_rows(lines: list[str]) -> list[str]:
    rows: list[str] = []
    for number, line in enumerate(lines, 1):
        if _RE_DEBUG_HEADER.match(line):
            body = f'<b class="h">{html.escape(line)}</b>'
        elif match := _RE_JSON_PAIR.match(line):
            value = match.group(3)
            flag = " on" if value.rstrip(",") == "true" else (" off" if value.rstrip(",") == "false" else "")
            body = (f'{match.group(1)}"<i class="k">{html.escape(match.group(2))}</i>": '
                    f'<i class="v{flag}">{html.escape(value)}</i>')
        elif (match := _RE_DEBUG_PAIR.match(line)) and match.group(3) is not None:
            value = match.group(3)[1:]
            flag = " on" if value == "1b" else (" off" if value == "0b" else "")
            body = (f'{html.escape(match.group(1))}<i class="k">{html.escape(match.group(2))}</i>: '
                    f'<i class="v{flag}">{html.escape(value)}</i>')
        else:
            body = html.escape(line)
        rows.append(f'<div class="r l-INFO" id="L{number}"><a href="#L{number}">{number}</a><span>{body}</span></div>')
    return rows


def _json_marks(lines: list[str]) -> list[tuple[int, str]]:
    """Top-level blocks of a pretty-printed JSON dump, with "sections" swapped for the sections inside it."""
    marks: list[tuple[int, str]] = []
    in_sections = False
    for number, line in enumerate(lines, 1):
        match = _RE_JSON_PAIR.match(line)
        if not match or match.group(3) not in ("{", "["):
            continue
        depth = len(match.group(1))
        if depth == 2:
            in_sections = match.group(2) == "sections"
            if not in_sections:
                marks.append((number, match.group(2)))
        elif depth == 4 and in_sections:
            marks.append((number, match.group(2)))
    return marks


def render(
    text: str,
    *,
    title: str,
    kind: str,
    facts: Sequence[tuple[str, str]] = (),
    marks: Sequence[tuple[int, str]] = (),
    now: datetime | None = None,
) -> str:
    """kind is "log" (level colors and filters) or "dmzdebug" (sections and key/value colors). marks are
    (line number, label) entries for the jump list; dmzdebug dumps list their sections automatically."""
    lines = text.splitlines()
    if kind == "dmzdebug":
        rows = _debug_rows(lines)
        if text.lstrip().startswith("{"):
            marks = _json_marks(lines)
        else:
            marks = [(i, line.strip("# :")) for i, line in enumerate(lines, 1) if line.startswith("# ")]
        mark_class = "sec"
    else:
        rows = _log_rows(lines)
        mark_class = "err"
    expires = (now or datetime.now(timezone.utc)) + timedelta(seconds=EXPIRY_SECONDS)
    fact_html = "".join(f"<span><b>{html.escape(k)}</b> {html.escape(v)}</span>" for k, v in facts if v)
    mark_html = "".join(
        f'<li><a class="{mark_class}" href="#L{n}"><code>{n}</code> {html.escape(label[:160])}</a></li>'
        for n, label in marks
    )
    jump = f'<nav><h2>{"Sections" if kind == "dmzdebug" else "Problems"}</h2><ol>{mark_html}</ol></nav>' if mark_html else ""
    filters = "".join(
        f'<label class="l-{lv}"><input type="checkbox" data-level="{lv}" checked> {lv}</label>' for lv in _LEVELS
    ) if kind == "log" else ""
    return _PAGE.format(
        title=html.escape(title),
        kind="DMZ debug dump" if kind == "dmzdebug" else "Log analysis",
        facts=fact_html,
        expires=expires.strftime("%Y-%m-%d %H:%M UTC"),
        count=len(lines),
        filters=filters,
        jump=jump,
        rows="\n".join(rows),
    )


async def save(settings: Any, page_html: str) -> str:
    """Writes the page and returns its public URL."""
    token = new_token()
    path = page_path(settings, token)

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page_html, encoding="utf-8")

    await asyncio.to_thread(write)
    return page_url(settings, token)


def read_live(settings: Any, token: str, *, now: float | None = None) -> bytes | None:
    """The page's bytes while it is under 24h old, else None."""
    try:
        path = page_path(settings, token)
        if path.stat().st_mtime + EXPIRY_SECONDS < (now or time.time()):
            return None
        return path.read_bytes()
    except (OSError, ValueError):
        return None


def purge(settings: Any, *, now: float | None = None) -> int:
    directory = Path(settings.log_page_dir)
    if not directory.is_dir():
        return 0
    cutoff = (now or time.time()) - EXPIRY_SECONDS
    removed = 0
    for path in directory.glob("*.html"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            log.exception("Could not purge log page", extra={"path": str(path)})
    return removed


# Self-contained: inline CSS/JS only, served under a sandbox CSP (routes_log_pages.py). Doubled braces are literal.
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>{title} · DragonMineZ logs</title>
<style>
:root{{--bg:#0f1115;--panel:#171a21;--line:#262a33;--fg:#d7dae0;--dim:#7d8590;--err:#ff6b6b;--errbg:rgba(255,80,80,.12);
--warn:#f5c04a;--info:#d7dae0;--debug:#7d8590;--key:#7cc4ff;--val:#c3e88d;--hit:#ffd86b;--accent:#f39c32}}
@media (prefers-color-scheme:light){{:root{{--bg:#fafafa;--panel:#fff;--line:#e3e5e8;--fg:#1f2328;--dim:#6e7781;
--err:#cf222e;--errbg:rgba(207,34,46,.08);--warn:#9a6700;--info:#1f2328;--debug:#6e7781;--key:#0550ae;--val:#116329;--hit:#bf8700}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}}
header{{position:sticky;top:0;z-index:2;background:var(--panel);border-bottom:1px solid var(--line);padding:12px 16px}}
h1{{margin:0 0 4px;font-size:18px}}h1 small{{color:var(--accent);font-weight:600;margin-left:8px;font-size:13px}}
.facts,.tools{{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--dim);font-size:13px}}.facts b{{color:var(--fg)}}
.tools{{margin-top:8px;align-items:center}}.tools input[type=search]{{flex:1;min-width:180px;max-width:360px;padding:5px 8px;
border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}}
.tools label{{cursor:pointer;user-select:none}}main{{display:flex;align-items:flex-start}}
nav{{position:sticky;top:110px;width:300px;flex:none;max-height:calc(100vh - 120px);overflow:auto;padding:12px 16px;
border-right:1px solid var(--line)}}nav h2{{font-size:13px;text-transform:uppercase;color:var(--dim);margin:0 0 6px}}
nav ol{{list-style:none;margin:0;padding:0}}nav li{{margin:0 0 6px}}nav a{{color:var(--fg);text-decoration:none;font-size:13px;
word-break:break-word}}nav a.err{{color:var(--err)}}nav a:hover{{text-decoration:underline}}nav code{{color:var(--dim)}}
#log{{flex:1;min-width:0;font:12.5px/1.5 ui-monospace,Consolas,monospace;padding:8px 0}}
.r{{display:flex;padding:0 16px 0 0}}.r>a{{flex:none;width:64px;text-align:right;padding-right:12px;color:var(--dim);
text-decoration:none;user-select:none}}.r>span{{white-space:pre-wrap;word-break:break-word;min-width:0}}
.r:target{{background:rgba(243,156,50,.18)}}.l-ERROR{{color:var(--err)}}.r.l-ERROR{{background:var(--errbg)}}
.l-WARN{{color:var(--warn)}}.l-DEBUG{{color:var(--debug)}}.tr{{opacity:.85}}.r.noise{{background:none;opacity:.5}}
.h{{color:var(--accent)}}.k{{color:var(--key);font-style:normal}}.v{{color:var(--val);font-style:normal}}
.v.on{{color:var(--val);font-weight:700}}.v.off{{color:var(--dim)}}
.hide-ERROR .r.l-ERROR,.hide-WARN .r.l-WARN,.hide-INFO .r.l-INFO,.hide-DEBUG .r.l-DEBUG,.r.miss{{display:none}}
mark{{background:var(--hit);color:#000}}
@media (max-width:760px){{main{{display:block}}nav{{position:static;width:auto;max-height:240px;border-right:0;
border-bottom:1px solid var(--line)}}.r>a{{width:44px;padding-right:8px}}}}
</style></head><body>
<header><h1>{title}<small>{kind}</small></h1><div class="facts">{facts}<span>{count} lines</span>
<span>Personal paths and IPs are masked</span><span>Expires {expires}</span></div>
<div class="tools"><input type="search" id="q" placeholder="Search the log" aria-label="Search the log">
<span id="hits"></span>{filters}</div></header>
<main>{jump}<div id="log">
{rows}
</div></main>
<script>
const body=document.body,rows=[...document.querySelectorAll('#log .r')],hits=document.getElementById('hits');
document.querySelectorAll('[data-level]').forEach(box=>box.addEventListener('change',()=>
  body.classList.toggle('hide-'+box.dataset.level,!box.checked)));
let timer;document.getElementById('q').addEventListener('input',e=>{{clearTimeout(timer);timer=setTimeout(()=>{{
  const q=e.target.value.trim().toLowerCase();let n=0;
  for(const r of rows){{const miss=q&&!r.lastChild.textContent.toLowerCase().includes(q);r.classList.toggle('miss',!!miss);if(q&&!miss)n++}}
  hits.textContent=q?n+' matching lines':'';}},150)}});
</script></body></html>
"""
