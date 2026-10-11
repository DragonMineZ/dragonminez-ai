"""Public 24h log viewer pages at /l/<token> (the logs hostname on the tunnel). No login: the token is the key."""

import asyncio

from aiohttp import web

from bulmaai.services import log_pages
from bulmaai.services.ticket_pages import TOKEN_PATTERN
from bulmaai.web.core import BOT

routes = web.RouteTableDef()

# The page only has inline CSS/JS. `sandbox` (no allow-same-origin) gives it an opaque origin, so a log's content
# can never reach the panel's cookies or API even though the panel serves it.
HEADERS = {
    "Content-Security-Policy": (
        "sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Cache-Control": "private, no-store",
    "X-Robots-Tag": "noindex, nofollow",
}


@routes.get("/l/{token:" + TOKEN_PATTERN + "}")
async def hosted_log(request: web.Request) -> web.Response:
    body = await asyncio.to_thread(log_pages.read_live, request.app[BOT].settings, request.match_info["token"])
    if body is None:
        raise web.HTTPNotFound(text="This log doesn't exist or has expired (logs are kept for 24 hours).")
    return web.Response(body=body, content_type="text/html", charset="utf-8", headers=HEADERS)
