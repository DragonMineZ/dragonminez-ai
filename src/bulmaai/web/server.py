"""aiohttp app for the admin panel. Runs on the bot's event loop; cloudflared terminates TLS in front."""

import hashlib
import hmac
import logging
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

import discord
from aiohttp import web

from bulmaai.services.discord_oauth import DiscordOAuthClient, build_discord_authorization_url
from bulmaai.web import (
    routes_announce,
    routes_logs,
    routes_moderation,
    routes_patreon,
    routes_presets,
    routes_settings,
    routes_status,
    routes_tickets,
    routes_transcripts,
)
from bulmaai.web.core import (
    BOT,
    PERMISSIONS,
    SESSION_COOKIE,
    REMEMBER_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    Actor,
    Tier,
    audit,
    panel_guild,
    requires,
    resolve_member,
    sign_session,
    tier_for,
    user_json,
)


log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE_COOKIE = "panel_oauth_state"
REMEMBER_COOKIE = "panel_remember"
MODULES = (
    routes_status,
    routes_logs,
    routes_settings,
    routes_moderation,
    routes_tickets,
    routes_transcripts,
    routes_patreon,
    routes_presets,
    routes_announce,
)

CSP = (
    "default-src 'self'; img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


@web.middleware
async def security_middleware(request: web.Request, handler) -> web.StreamResponse:
    # The tickets hostname shares this process (and tunnel) with the panel but may only ever serve /t/<token>.
    settings = request.app[BOT].settings
    transcript_host = urlparse(settings.ticket_transcript_public_url).netloc
    if request.host == transcript_host != urlparse(settings.panel_public_url).netloc:
        if not request.path.startswith("/t/"):
            return web.Response(status=404, text="Not found.")
    # Cross-site write protection: every non-GET must come from this origin. The public URL's host
    # is accepted too, so it works behind proxies/tunnels that rewrite Host (e.g. cloudflared).
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin")
        allowed = {request.host, urlparse(request.app[BOT].settings.panel_public_url).netloc}
        if not origin or urlparse(origin).netloc not in allowed:
            return web.json_response({"error": "Cross-origin request rejected."}, status=403)
    try:
        response = await handler(request)
    except web.HTTPException as error:
        _harden(request, error)
        raise
    except Exception:
        log.exception("Admin panel error on %s %s", request.method, request.path)
        response = web.json_response({"error": "Internal error, check the bot logs."}, status=500)
    _harden(request, response)
    return response


def _harden(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("Content-Security-Policy", CSP)  # hosted transcripts bring their own
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # revalidate via ETag; index.html versions URLs


def _cookie_secure(bot: discord.Bot) -> bool:
    return bot.settings.panel_public_url.startswith("https://")


def _redirect_uri(bot: discord.Bot) -> str:
    return bot.settings.panel_public_url.rstrip("/") + "/auth/callback"


def _versioned_index() -> str:
    # Stamp every /static/ URL with a content hash so a CDN or browser can never pair a cached old
    # panel.js with newer module files. Computed once per process; a deploy restarts the bot.
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.iterdir()):
        if path.is_file():
            digest.update(path.name.encode() + path.read_bytes())
    version = digest.hexdigest()[:12]
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return re.sub(r'((?:src|href)="/static/[^"?]+)"', lambda m: f'{m[1]}?v={version}"', html)


INDEX_HTML = _versioned_index()


async def index(request: web.Request) -> web.StreamResponse:
    return web.Response(text=INDEX_HTML, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def login(request: web.Request) -> web.StreamResponse:
    bot = request.app[BOT]
    state = secrets.token_urlsafe(24)
    url = build_discord_authorization_url(
        client_id=str(bot.settings.discord_oauth_client_id),
        redirect_uri=_redirect_uri(bot),
        state=state,
    )
    response = _redirect(url)
    response.set_cookie(
        STATE_COOKIE, state, max_age=600, httponly=True, secure=_cookie_secure(bot), samesite="Lax", path="/auth"
    )
    if request.query.get("remember") == "1":
        response.set_cookie(
            REMEMBER_COOKIE, "1", max_age=600, httponly=True, secure=_cookie_secure(bot), samesite="Lax", path="/auth"
        )
    return response


def _redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location})


def _plain_page(message: str, status: int) -> web.Response:
    # Built without user input; message is always a constant from this module.
    body = (
        "<!doctype html><meta charset=utf-8><title>BulmaAI Panel</title>"
        f"<link rel=stylesheet href=/static/panel.css><main class=plain><p>{message}</p>"
        "<p><a href=/>Back to the panel</a></p></main>"
    )
    return web.Response(text=body, content_type="text/html", status=status)


async def callback(request: web.Request) -> web.StreamResponse:
    bot = request.app[BOT]
    expected = request.cookies.get(STATE_COOKIE, "")
    state = request.query.get("state", "")
    code = request.query.get("code")
    if not code or not expected or not hmac.compare_digest(state.encode(), expected.encode()):
        return _plain_page("Login expired or was tampered with. Try again.", 400)

    client = DiscordOAuthClient(
        client_id=str(bot.settings.discord_oauth_client_id),
        client_secret=bot.settings.discord_oauth_client_secret or "",
        redirect_uri=_redirect_uri(bot),
    )
    try:
        user_id = await client.fetch_user_id_for_code(code)
    except Exception as error:
        log.warning("Admin panel Discord OAuth exchange failed: %s", error)
        return _plain_page("Discord login failed. Try again.", 502)

    guild = panel_guild(bot)
    member = await resolve_member(guild, user_id) if guild else None
    tier = tier_for(member, bot.settings) if member else Tier.NONE
    if member is None or tier == Tier.NONE:
        log.warning("Admin panel login refused for non-staff user %s", user_id)
        return _plain_page("This panel is for DragonMineZ staff only.", 403)

    await audit(Actor(member=member, tier=tier), "auth.login")
    ttl = REMEMBER_TTL_SECONDS if request.cookies.get(REMEMBER_COOKIE) == "1" else SESSION_TTL_SECONDS
    response = _redirect("/")
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(bot.settings.panel_session_secret or "", user_id, ttl=ttl),
        max_age=ttl,
        httponly=True,
        secure=_cookie_secure(bot),
        samesite="Lax",
        path="/",
    )
    response.del_cookie(STATE_COOKIE, path="/auth")
    response.del_cookie(REMEMBER_COOKIE, path="/auth")
    return response


async def logout(request: web.Request) -> web.StreamResponse:
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


core_routes = web.RouteTableDef()


@core_routes.get("/api/me")
@requires("status.view")
async def me(request: web.Request, actor: Actor) -> web.Response:
    guild = actor.member.guild
    return web.json_response(
        {
            "user": user_json(actor.member),
            "tier": actor.tier.name.lower(),
            "tier_level": int(actor.tier),
            "permissions": actor.permissions,
            "tiers": {name: tier.name.lower() for name, tier in PERMISSIONS.items()},
            "guild": {"id": str(guild.id), "name": guild.name, "icon": guild.icon.url if guild.icon else None},
        }
    )


@core_routes.get("/api/guild")
@requires("status.view")
async def guild_meta(request: web.Request, actor: Actor) -> web.Response:
    """Channel and role lists for pickers and for rendering IDs as names."""
    guild = actor.member.guild
    channels = [
        {
            "id": str(channel.id),
            "name": channel.name,
            "type": str(channel.type),
            "category": channel.category.name if getattr(channel, "category", None) else None,
            "position": channel.position,
        }
        for channel in guild.channels
    ]
    roles = [
        {"id": str(role.id), "name": role.name, "color": str(role.colour), "position": role.position}
        for role in reversed(guild.roles)
        if not role.is_default()
    ]
    return web.json_response({"channels": channels, "roles": roles})


def create_app(bot: discord.Bot) -> web.Application:
    app = web.Application(middlewares=[security_middleware], client_max_size=2 * 1024 * 1024)
    app[BOT] = bot
    app.router.add_get("/", index)
    app.router.add_get("/auth/login", login)
    app.router.add_get("/auth/callback", callback)
    app.router.add_post("/auth/logout", logout)
    app.router.add_static("/static/", STATIC_DIR, append_version=False)
    app.add_routes(core_routes)
    for module in MODULES:
        app.add_routes(module.routes)
    return app


class PanelServer:
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        settings = self.bot.settings
        self._runner = web.AppRunner(create_app(self.bot), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, settings.panel_host, settings.panel_port).start()
        log.info("Admin panel listening on %s:%s (%s)", settings.panel_host, settings.panel_port, settings.panel_public_url)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

