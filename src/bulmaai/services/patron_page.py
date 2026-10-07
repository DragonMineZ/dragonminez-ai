"""Minecraft-styled pages for the Patreon / beta-access browser flow (downloads.dragonminez.com).

Every page is the same panel over the DragonMineZ art: a title, an optional step tracker and Discord
account card, a body, buttons and the legal footer. The tone picks Minecraft's chat color for the page.
"""

import html
from dataclasses import dataclass
from pathlib import Path

from bulmaai.services.release_webhook import ReleaseWebhookHttpResponse


ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "patron"
ASSET_PREFIX = "/beta-access/assets/"
ASSET_TYPES = {"bg.webp": "image/webp", "minecraft.woff": "font/woff"}

DOWNLOADS_CHANNEL_URL = "https://discord.com/channels/1216429657273012415/1516564287210913932"
PATREON_URL = "https://www.patreon.com/DragonMineZ"
PATREON_LOGOUT_URL = "https://www.patreon.com/logout"
TERMS_URL = "https://github.com/DragonMineZ/dragonminez-ai/blob/main/TERMS_OF_SERVICE.md"
PRIVACY_URL = "https://github.com/DragonMineZ/dragonminez-ai/blob/main/PRIVACY_POLICY.md"

# Minecraft chat colors (§6 gold, §a green, §b aqua, §c red) and their shadow shades.
TONES = {
    "gold": ("#ffaa00", "#3f2a00"),
    "green": ("#55ff55", "#153f15"),
    "aqua": ("#55ffff", "#153f3f"),
    "red": ("#ff5555", "#3f1515"),
}
STEP_NAMES = ("Discord", "Patreon", "Whitelisted")


@dataclass(frozen=True, slots=True)
class DiscordCard:
    display_name: str
    username: str
    avatar_url: str | None = None


def asset_response(path: str) -> ReleaseWebhookHttpResponse | None:
    name = path.removeprefix(ASSET_PREFIX)
    if name not in ASSET_TYPES:
        return None
    return ReleaseWebhookHttpResponse(
        status=200,
        body=b"",
        content_type=ASSET_TYPES[name],
        headers=(("Cache-Control", "public, max-age=604800"),),
        file_path=ASSET_DIR / name,
    )


def link(label: str, href: str) -> str:
    return f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener">{html.escape(label)}</a>'


def button(label: str, href: str, *, new_tab: bool = False) -> str:
    target = ' target="_blank" rel="noopener"' if new_tab else ""
    return f'<a class="btn" href="{html.escape(href, quote=True)}"{target}>{html.escape(label)}</a>'


def form_button(label: str, fields: dict[str, str]) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(name, quote=True)}" value="{html.escape(value, quote=True)}">'
        for name, value in fields.items()
    )
    return f'<form method="get">{hidden}<button class="btn" type="submit">{html.escape(label)}</button></form>'


def _steps(done: tuple[bool, bool, bool]) -> str:
    parts = []
    for index, name in enumerate(STEP_NAMES):
        if index:
            parts.append(f'<span class="line{" done" if done[index] else ""}"></span>')
        state = "done" if done[index] else ("now" if index == 0 or done[index - 1] else "")
        mark = "&#10004;" if done[index] else ""
        parts.append(f'<span class="step {state}"><span class="box">{mark}</span>{name}</span>')
    return f'<div class="steps">{"".join(parts)}</div>'


def _card(card: DiscordCard) -> str:
    initial = html.escape((card.display_name or "?")[:1].upper())
    avatar = (
        f'<img class="avatar" src="{html.escape(card.avatar_url, quote=True)}" alt="">'
        if card.avatar_url
        else f'<div class="avatar">{initial}</div>'
    )
    return (
        f'<div class="who">{avatar}<div><div class="name">{html.escape(card.display_name)}</div>'
        f'<div class="handle">@{html.escape(card.username)}</div></div></div>'
    )


def render_page(
    *,
    title: str,
    body_html: str,
    tone: str = "gold",
    steps: tuple[bool, bool, bool] | None = None,
    card: DiscordCard | None = None,
    actions: str = "",
    note_html: str = "",
    switch_url: str | None = None,
) -> bytes:
    """body_html, actions and note_html are trusted markup: escape anything user-supplied before passing it."""
    accent, shadow = TONES[tone]
    main = (
        f'<h1>{html.escape(title)}</h1>'
        f'{_steps(steps) if steps else ""}{_card(card) if card else ""}'
        f'<p>{body_html}</p>{actions}'
        f'{f"<p class=note>{note_html}</p>" if note_html else ""}'
    )
    # The switch-account panel is a :target section, so "click here" swaps panels without any script.
    switch = ""
    if switch_url:
        switch = (
            '<section class="panel" id="switch"><h1>Switch account</h1>'
            "<p>To use another account, make sure you sign out of your current account and switch to the one "
            "you have a valid pledge on. Then link again with the right one.</p>"
            f'{button("1. Sign out of Patreon", PATREON_LOGOUT_URL, new_tab=True)}'
            f'{button("2. Link another account", switch_url)}{_LEGAL}</section>'
        )
    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>DragonMineZ Beta Access</title><style>{_CSS}:root{{--accent:{accent};--shadow:{shadow}}}</style>"
        f'</head><body>{switch}<main class="panel">{_BALL}{main}{_LEGAL}</main>{_SHENRON}{_SCRIPT}</body></html>'
    )
    return page.encode("utf-8")


def page_response(*, status: int = 200, **kwargs) -> ReleaseWebhookHttpResponse:
    return ReleaseWebhookHttpResponse(
        status=status, body=render_page(**kwargs), content_type="text/html; charset=utf-8"
    )


_LEGAL = f'<p class="legal">{link("Terms of Service", TERMS_URL)} · {link("Privacy Policy", PRIVACY_URL)}</p>'

_CSS = (
    f'@font-face{{font-family:Minecraft;src:url("{ASSET_PREFIX}minecraft.woff") format("woff");font-display:swap}}'
    """
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{font-family:Minecraft,"Courier New",monospace;color:#fff;color-scheme:dark;min-height:100vh;display:flex;align-items:center;
padding:24px 16px;-webkit-font-smoothing:none;background:#000 url("/beta-access/assets/bg.webp") center/cover no-repeat fixed}
body::before{content:"";position:fixed;inset:0;pointer-events:none;background:linear-gradient(90deg,rgba(0,0,0,.75),rgba(0,0,0,.35) 55%,rgba(0,0,0,.1))}
.panel{position:relative;width:min(430px,100%);margin-left:max(0px,6vw);padding:26px 24px 22px;background:rgba(16,12,20,.9);
border:2px solid #100010;box-shadow:inset 0 0 0 2px var(--shadow),inset 0 0 0 4px #100010}
#switch{display:none}#switch:target{display:block}#switch:target~main{display:none}
h1{font:inherit;font-size:32px;line-height:1.1;margin:0 0 16px;color:var(--accent);text-shadow:3px 3px 0 var(--shadow)}
p{font-size:15px;line-height:1.6;color:#e0e0e0;margin:0 0 18px;text-shadow:2px 2px 0 #2a2a2a}
p b{color:var(--accent);font-weight:400}
.panel a:not(.btn){color:#7f7fff;text-shadow:1px 1px 0 #15153f}.panel a:not(.btn):hover{color:#aaaaff}
.who{display:flex;align-items:center;gap:14px;margin:0 0 18px;padding:10px;background:rgba(0,0,0,.45);border:2px solid #2b2b2b}
.avatar{width:52px;height:52px;flex:none;object-fit:cover;background:#5865f2;display:grid;place-items:center;font-size:24px;border:2px solid var(--accent)}
.name{font-size:16px;text-shadow:2px 2px 0 #3f3f3f}.handle{font-size:11px;color:#aaa;margin-top:4px}
.steps{display:flex;align-items:center;gap:8px;margin:0 0 18px;font-size:11px;color:#aaa}
.step{display:flex;align-items:center;gap:6px}
.box{width:16px;height:16px;border:2px solid #555;background:#1b1b1b;display:grid;place-items:center;font-size:10px}
.step.done{color:#fff}.step.done .box{background:var(--accent);border-color:var(--accent);color:#000}.step.now .box{border-color:var(--accent)}
.line{flex:1;height:2px;background:#444}.line.done{background:var(--accent)}
form{margin:0}
.btn{display:block;width:100%;padding:12px 10px;font:inherit;font-size:16px;color:#fff;text-align:center;text-decoration:none;cursor:pointer;
background:#6f6f6f;border:2px solid #000;box-shadow:inset 2px 2px 0 #aaa,inset -2px -3px 0 #3a3a3a;text-shadow:2px 2px 0 #3f3f3f}
.btn:hover,.btn:focus-visible{background:#7e86c4;box-shadow:inset 2px 2px 0 #bdc6ff,inset -2px -3px 0 #4a5394;color:#ffffa0;outline:none}
.btn:active{box-shadow:inset 2px 2px 0 #3a3a3a,inset -2px -2px 0 #aaa}
.btn+.btn{margin-top:8px}
.note{font-size:11px;color:#aaa;margin:14px 0 0;text-shadow:1px 1px 0 #000}
.legal{font-size:11px;margin:16px 0 0}
.ball{position:absolute;top:-18px;right:12px;width:36px;height:36px;cursor:pointer}
.shenron{position:fixed;inset:0;z-index:9;display:none;place-items:center;background:rgba(0,12,0,.94);text-align:center;padding:16px;cursor:pointer}
.shenron.on{display:grid}.shenron svg{width:min(360px,80vw)}
.shenron p{color:#55ff55;text-shadow:2px 2px 0 #153f15;font-size:18px;margin-top:16px}
@media (max-width:700px){body{justify-content:center}body::before{background:rgba(0,0,0,.6)}.panel{margin-left:0}}
"""
)

# 8x8 pixel four-star dragon ball; seven clicks summon Shenron.
_BALL = (
    '<svg class="ball" id="ball" viewBox="0 0 8 8" shape-rendering="crispEdges" aria-hidden="true">'
    '<rect x="2" y="0" width="4" height="8" fill="#ff8c00"/><rect x="0" y="2" width="8" height="4" fill="#ff8c00"/>'
    '<rect x="1" y="1" width="6" height="6" fill="#ff8c00"/><rect x="2" y="1" width="2" height="1" fill="#ffd27a"/>'
    '<rect x="1" y="2" width="1" height="1" fill="#ffd27a"/><rect x="3" y="3" width="1" height="1" fill="#d0201a"/>'
    '<rect x="5" y="3" width="1" height="1" fill="#d0201a"/><rect x="3" y="5" width="1" height="1" fill="#d0201a"/>'
    '<rect x="5" y="5" width="1" height="1" fill="#d0201a"/><rect x="6" y="5" width="1" height="2" fill="#c05a00"/>'
    '<rect x="2" y="7" width="4" height="1" fill="#c05a00"/></svg>'
)
_SHENRON = (
    '<div class="shenron" id="shenron"><div><svg viewBox="0 0 16 10" shape-rendering="crispEdges" aria-hidden="true">'
    '<g fill="#2e8b3a"><rect x="2" y="2" width="10" height="5"/><rect x="12" y="3" width="3" height="3"/>'
    '<rect x="0" y="4" width="2" height="5"/><rect x="4" y="0" width="1" height="2"/><rect x="8" y="0" width="1" height="2"/></g>'
    '<g fill="#55ff55"><rect x="2" y="2" width="10" height="1"/><rect x="12" y="3" width="3" height="1"/></g>'
    '<rect x="9" y="3" width="1" height="1" fill="#ff5555"/><rect x="13" y="5" width="2" height="1" fill="#fff"/>'
    '<rect x="5" y="7" width="7" height="1" fill="#1b5e24"/></svg>'
    "<p>\"Your wish is granted… beta access was already yours.\"</p></div></div>"
)
_SCRIPT = (
    "<script>(()=>{let n=0;const s=document.getElementById('shenron');"
    "document.getElementById('ball').onclick=()=>{if(++n>=7){n=0;s.classList.add('on')}};"
    "s.onclick=()=>s.classList.remove('on')})()</script>"
)
