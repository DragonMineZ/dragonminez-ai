"""Public hosted ticket transcripts at /t/<token> (the tickets hostname on the tunnel). No login: the token is the key."""

import asyncio

from aiohttp import web

from bulmaai.services.ticket_pages import TOKEN_PATTERN, is_servable, page_path
from bulmaai.web.core import BOT

routes = web.RouteTableDef()

# chat-exporter pages carry inline styles/scripts and load dayjs/fonts (jsDelivr), highlight.js (cdnjs) and tippy
# (unpkg); images come from Discord's CDN or are inlined. `sandbox` (without allow-same-origin) makes the page an
# opaque origin, so nothing in a transcript can touch the panel's cookies or API even if it is served from the
# panel's own hostname.
TRANSCRIPT_CSP = (
    "sandbox allow-scripts allow-popups; default-src 'none'; img-src data: https:; media-src https:; "
    "style-src 'unsafe-inline'; font-src https://cdn.jsdelivr.net; "
    "script-src 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Content-Security-Policy": TRANSCRIPT_CSP,
    "Cache-Control": "private, no-store",
    "X-Robots-Tag": "noindex, nofollow",
}


@routes.get("/t/{token:" + TOKEN_PATTERN + "}")
async def hosted_transcript(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    if not await is_servable(token):
        raise web.HTTPNotFound(text="This transcript doesn't exist or has expired.")
    path = page_path(request.app[BOT].settings, token)
    try:
        body = await asyncio.to_thread(path.read_bytes)
    except OSError:
        raise web.HTTPNotFound(text="This transcript doesn't exist or has expired.")
    return web.Response(body=body, content_type="text/html", charset="utf-8", headers=HEADERS)
