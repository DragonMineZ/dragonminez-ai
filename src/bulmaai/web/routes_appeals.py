"""The appeals website (the appeals hostname on the panel's tunnel, everything under /appeal/). A banned or timed-out
member logs in with Discord (identify scope, state checked against an HttpOnly cookie), sees why, and sends one
appeal; staff answer on its card (cogs/ban_appeals.py) and the member reads the outcome here, since the bot can't DM
someone who shares no server with it. The OAuth access token is used once and never stored."""

import hashlib
import hmac
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from aiohttp import web

from bulmaai.services import ban_appeals as store
from bulmaai.services import mod_cases
from bulmaai.services.discord_oauth import DiscordOAuthClient, build_discord_authorization_url
from bulmaai.web import appeal_pages as pages
from bulmaai.web.core import BOT, panel_guild, read_session, resolve_member, sign_session

log = logging.getLogger(__name__)

routes = web.RouteTableDef()

PREFIX = "/appeal/"
SESSION_COOKIE = "appeal_session"
STATE_COOKIE = "appeal_oauth_state"
SESSION_TTL_SECONDS = 2 * 3600
HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "same-origin"}
# ponytail: in-memory per-IP limits (reset on restart); the DB rules (one open appeal, cooldown) are the real guard.
RATE_LIMITS = {"get": (40, 60), "post": (8, 600)}  # kind -> (requests, window seconds)
_hits: dict[str, deque] = {}


def host(settings) -> str:
    return urlparse(settings.appeals_public_url or "").netloc


def _secret(settings) -> str:
    # Its own key, so an appeals cookie is never a valid panel session.
    return (settings.panel_session_secret or "") + ":appeals"


def _redirect_uri(settings) -> str:
    return settings.appeals_public_url.rstrip("/") + PREFIX + "callback"


def _secure(settings) -> bool:
    return settings.appeals_public_url.startswith("https://")


def csrf_token(settings, session_value: str) -> str:
    return hmac.new(_secret(settings).encode(), b"csrf." + session_value.encode(), hashlib.sha256).hexdigest()[:40]


def _client_ip(request: web.Request) -> str:
    return request.headers.get("CF-Connecting-IP") or request.remote or "?"


def rate_limited(request: web.Request, kind: str, *, now: float | None = None) -> bool:
    limit, window = RATE_LIMITS[kind]
    now = time.monotonic() if now is None else now
    if len(_hits) > 10000:
        _hits.clear()
    hits = _hits.setdefault(f"{kind}:{_client_ip(request)}", deque())
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


def _html(text: str, status: int = 200) -> web.Response:
    return web.Response(text=text, content_type="text/html", charset="utf-8", status=status, headers=HEADERS)


def _redirect(location: str) -> web.Response:
    return web.Response(status=303, headers={"Location": location, **HEADERS})


@dataclass(frozen=True)
class Visitor:
    lang: str
    settings: object
    user_id: int | None
    csrf: str | None


def _visitor(request: web.Request) -> Visitor:
    settings = request.app[BOT].settings
    value = request.cookies.get(SESSION_COOKIE) or ""
    user_id = read_session(_secret(settings), value) if value else None
    return Visitor(
        lang=pages.pick_language(request.headers.get("Accept-Language")),
        settings=settings,
        user_id=user_id,
        csrf=csrf_token(settings, value) if user_id is not None else None,
    )


async def _gate(request: web.Request, kind: str) -> tuple[Visitor, web.Response | None]:
    """Only the appeals hostname serves these pages; then setup, the on/off switch and the rate limit."""
    visitor = _visitor(request)
    settings = visitor.settings
    if not host(settings) or request.host != host(settings):
        raise web.HTTPNotFound(text="Not found.")
    if rate_limited(request, kind):
        return visitor, _html(pages.message(visitor.lang, "rate"), 429)
    if not store.login_ready(settings):
        return visitor, _html(pages.message(visitor.lang, "not_setup"), 503)
    if not settings.appeals_channel_id:
        return visitor, _html(pages.message(visitor.lang, "closed"), 503)
    return visitor, None


@dataclass(frozen=True)
class Punishment:
    kind: str  # ban | timeout
    user_name: str
    reason: str | None
    at: datetime | None
    until: datetime | None = None


async def _latest_case(guild_id: int, user_id: int, action: str):
    try:
        cases = await mod_cases.list_cases(guild_id, user_id=user_id, limit=25)
    except Exception:
        log.warning("Couldn't load cases for an appeal page", exc_info=True)
        return None
    return next((case for case in cases if case.action == action), None)


async def find_punishment(bot: discord.Bot, user_id: int) -> Punishment | None:
    """A ban in the main guild, else a timeout still running. Raises LookupError when Discord can't be asked."""
    guild = panel_guild(bot)
    if guild is None:
        raise LookupError("main guild not available")
    try:
        ban = await guild.fetch_ban(discord.Object(id=user_id))
    except discord.NotFound:
        ban = None
    except discord.HTTPException as error:
        raise LookupError("ban lookup failed") from error
    if ban is not None:
        case = await _latest_case(guild.id, user_id, "ban")
        return Punishment("ban", str(ban.user), ban.reason, case.created_at if case else None)
    member = await resolve_member(guild, user_id)
    until = getattr(member, "communication_disabled_until", None) if member is not None else None
    if until is None or until <= datetime.now(timezone.utc):
        return None
    case = await _latest_case(guild.id, user_id, "timeout")
    return Punishment("timeout", str(member), case.reason if case else None, case.created_at if case else None, until)


# --- pages -----------------------------------------------------------------------------------------------------------


@routes.get(PREFIX + "style.css")
async def style(request: web.Request) -> web.Response:
    if request.host != host(request.app[BOT].settings):
        raise web.HTTPNotFound(text="Not found.")
    return web.Response(text=pages.STYLE, content_type="text/css", headers={"Cache-Control": "public, max-age=3600"})


@routes.get(PREFIX)
async def home(request: web.Request) -> web.Response:
    visitor, refused = await _gate(request, "get")
    if refused:
        return refused
    if visitor.user_id is None:
        return _html(pages.landing(visitor.lang))
    return await _home(request, visitor)


async def _home(request: web.Request, visitor: Visitor, *, error: str | None = None, why: str = "", extra: str = "") -> web.Response:
    bot = request.app[BOT]
    settings = visitor.settings
    lang, user_id = visitor.lang, visitor.user_id
    try:
        latest = await store.latest(user_id)
        if latest is not None and latest.outcome is None:
            if latest.card_message_id is None:  # the card didn't go out when it was sent: try again
                await store.post_card(bot, latest)
            return _html(_status(visitor, latest, error=error))
        punishment = await find_punishment(bot, user_id)
    except Exception:
        log.exception("Appeals page failed", extra={"event": "ban_appeal_page_failed", "user_id": user_id})
        return _html(pages.message(lang, "error", retry=True), 503)
    if punishment is None:
        if latest is not None and latest.outcome == "accepted":
            return _html(_status(visitor, latest))
        return _html(pages.nothing(lang, settings.appeals_invite_url, visitor.csrf))
    if store.next_allowed(latest, settings.appeals_cooldown_days):
        return _html(_status(visitor, latest))
    return _html(
        pages.form(
            lang,
            csrf=visitor.csrf,
            kind=punishment.kind,
            reason=punishment.reason,
            at=punishment.at,
            until=punishment.until,
            why_max=store.WHY_MAX,
            extra_max=store.EXTRA_MAX,
            error=error,
            why=why,
            extra=extra,
        )
    )


def _status(visitor: Visitor, appeal: store.Appeal, *, error: str | None = None) -> str:
    return pages.status(
        visitor.lang,
        appeal,
        csrf=visitor.csrf,
        invite=visitor.settings.appeals_invite_url,
        reply_max=store.REPLY_MAX,
        again_on=store.next_allowed(appeal, visitor.settings.appeals_cooldown_days),
        error=error,
    )


# --- login -----------------------------------------------------------------------------------------------------------


@routes.get(PREFIX + "login")
async def login(request: web.Request) -> web.Response:
    visitor, refused = await _gate(request, "get")
    if refused:
        return refused
    settings = visitor.settings
    state = secrets.token_urlsafe(24)
    url = build_discord_authorization_url(
        client_id=str(settings.discord_oauth_client_id), redirect_uri=_redirect_uri(settings), state=state
    )
    response = web.Response(status=302, headers={"Location": url, **HEADERS})
    response.set_cookie(
        STATE_COOKIE, state, max_age=600, httponly=True, secure=_secure(settings), samesite="Lax", path=PREFIX
    )
    return response


@routes.get(PREFIX + "callback")
async def callback(request: web.Request) -> web.Response:
    visitor, refused = await _gate(request, "get")
    if refused:
        return refused
    settings = visitor.settings
    expected = request.cookies.get(STATE_COOKIE, "")
    state = request.query.get("state", "")
    code = request.query.get("code")
    if not code or not expected or not hmac.compare_digest(state.encode(), expected.encode()):
        return _html(pages.message(visitor.lang, "login_failed", retry=True), 400)
    client = DiscordOAuthClient(
        client_id=str(settings.discord_oauth_client_id),
        client_secret=settings.discord_oauth_client_secret or "",
        redirect_uri=_redirect_uri(settings),
    )
    try:
        user_id = await client.fetch_user_id_for_code(code)
    except Exception as error:
        log.info("Appeals Discord login failed: %s", error)
        return _html(pages.message(visitor.lang, "login_failed", retry=True), 502)
    response = _redirect(PREFIX)
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(_secret(settings), user_id, ttl=SESSION_TTL_SECONDS),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_secure(settings),
        samesite="Lax",
        path=PREFIX,
    )
    response.del_cookie(STATE_COOKIE, path=PREFIX)
    return response


async def _posted(request: web.Request) -> tuple[Visitor, dict, web.Response | None]:
    visitor, refused = await _gate(request, "post")
    if refused:
        return visitor, {}, refused
    form = await request.post()
    token = str(form.get("csrf") or "")
    if visitor.user_id is None or not visitor.csrf or not hmac.compare_digest(token.encode(), visitor.csrf.encode()):
        return visitor, {}, _html(pages.message(visitor.lang, "expired", retry=True), 403)
    return visitor, form, None


@routes.post(PREFIX + "logout")
async def logout(request: web.Request) -> web.Response:
    _visitor_, _form, refused = await _posted(request)
    if refused:
        return refused
    response = _redirect(PREFIX)
    response.del_cookie(SESSION_COOKIE, path=PREFIX)
    return response


# --- appeal and reply ------------------------------------------------------------------------------------------------


@routes.post(PREFIX + "submit")
async def submit(request: web.Request) -> web.Response:
    visitor, form, refused = await _posted(request)
    if refused:
        return refused
    bot = request.app[BOT]
    settings, lang, user_id = visitor.settings, visitor.lang, visitor.user_id
    t = pages.TEXT[lang]
    why = str(form.get("why") or "").strip()
    extra = str(form.get("extra") or "").strip()
    try:
        latest = await store.latest(user_id)
        if latest is not None and latest.outcome is None:
            return _redirect(PREFIX)
        if store.next_allowed(latest, settings.appeals_cooldown_days):
            return _redirect(PREFIX)
        punishment = await find_punishment(bot, user_id)
    except Exception:
        log.exception("Appeal submit failed", extra={"event": "ban_appeal_submit_failed", "user_id": user_id})
        return _html(pages.message(lang, "error", retry=True), 503)
    if punishment is None:
        return _redirect(PREFIX)
    if len(why) < store.WHY_MIN:
        return await _home(request, visitor, error=t["why_short"].format(min=store.WHY_MIN), why=why, extra=extra)
    if len(why) > store.WHY_MAX or len(extra) > store.EXTRA_MAX:
        return await _home(request, visitor, error=t["too_long"], why=why[: store.WHY_MAX], extra=extra[: store.EXTRA_MAX])
    try:
        appeal = await store.create(
            user_id=user_id,
            user_name=punishment.user_name,
            kind=punishment.kind,
            ban_reason=punishment.reason,
            punished_at=punishment.at,
            ends_at=punishment.until,
            answers={"why": why, **({"extra": extra} if extra else {})},
        )
    except Exception:
        log.exception("Couldn't save an appeal", extra={"event": "ban_appeal_save_failed", "user_id": user_id})
        return _html(pages.message(lang, "error", retry=True), 503)
    if appeal is None:  # a second tab got there first
        return _redirect(PREFIX)
    try:  # an appeal case keeps the case history and the weekly digest's count
        await mod_cases.record_case(
            guild_id=settings.panel_guild_id,
            user_id=user_id,
            moderator_id=None,
            action="appeal",
            reason=why[:1000],
            source="appeal",
        )
    except Exception:
        log.warning("Couldn't record appeal #%s as a case", appeal.id, exc_info=True)
    await store.post_card(bot, appeal)
    return _redirect(PREFIX)


@routes.post(PREFIX + "reply")
async def reply(request: web.Request) -> web.Response:
    visitor, form, refused = await _posted(request)
    if refused:
        return refused
    text = str(form.get("reply") or "").strip()
    appeal_id = str(form.get("appeal") or "")
    t = pages.TEXT[visitor.lang]
    if not appeal_id.isdigit():
        return _redirect(PREFIX)
    if not text or len(text) > store.REPLY_MAX:
        return await _home(request, visitor, error=t["answer_empty"] if not text else t["too_long"])
    try:
        appeal = await store.reply(int(appeal_id), visitor.user_id, text)
    except Exception:
        log.exception("Couldn't save an appeal reply", extra={"event": "ban_appeal_reply_failed"})
        return _html(pages.message(visitor.lang, "error", retry=True), 503)
    if appeal is not None:
        await store.note_reply(request.app[BOT], appeal)
    return _redirect(PREFIX)
