"""aiohttp app for the admin panel. Runs on the bot's event loop; nginx terminates TLS in front."""

import hmac
import logging
import secrets
from pathlib import Path
from urllib.parse import urlparse

import discord
from aiohttp import web

from bulmaai.services.discord_oauth import DiscordOAuthClient, build_discord_authorization_url
from bulmaai.web import (
    routes_moderation,
    routes_patreon,
    routes_presets,
    routes_settings,
    routes_status,
    routes_tickets,
)
from bulmaai.web.core import (
    BOT,
    PERMISSIONS,
    SESSION_COOKIE,
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
MODULES = (routes_status, routes_settings, routes_moderation, routes_tickets, routes_patreon, routes_presets)

CSP = (
    "default-src 'self'; img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


@web.middleware
async def security_middleware(request: web.Request, handler) -> web.StreamResponse:
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
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"


def _cookie_secure(bot: discord.Bot) -> bool:
    return bot.settings.panel_public_url.startswith("https://")


def _redirect_uri(bot: discord.Bot) -> str:
    return bot.settings.panel_public_url.rstrip("/") + "/auth/callback"


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


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
    if not code or not expected or not hmac.compare_digest(state, expected):
        return _plain_page("Login expired or was tampered with. Try again.", 400)

    client = DiscordOAuthClient(
        client_id=str(bot.settings.discord_oauth_client_id),
        client_secret=bot.settings.discord_oauth_client_secret or "",
        redirect_uri=_redirect_uri(bot),
    )
    try:
        user_id = await client.fetch_user_id_for_code(code)
    except Exception:
        log.exception("Admin panel Discord OAuth exchange failed")
        return _plain_page("Discord login failed. Try again.", 502)

    guild = panel_guild(bot)
    member = await resolve_member(guild, user_id) if guild else None
    tier = tier_for(member, bot.settings) if member else Tier.NONE
    if member is None or tier == Tier.NONE:
        log.warning("Admin panel login refused for non-staff user %s", user_id)
        return _plain_page("This panel is for DragonMineZ staff only.", 403)

    await audit(Actor(member=member, tier=tier), "auth.login")
    response = _redirect("/")
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(bot.settings.panel_session_secret or "", user_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(bot),
        samesite="Lax",
        path="/",
    )
    response.del_cookie(STATE_COOKIE, path="/auth")
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

