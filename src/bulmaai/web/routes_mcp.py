"""MCP server for Staff Bulma: staff connect their own Claude to our tools (Streamable HTTP, stateless, JSON only).
Bearer-token auth only (never the panel cookie). Lookups run at once; action tools only create a pending action that
the staff member confirms with a button in their Discord DMs."""

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

from aiohttp import web

from bulmaai.services import staff_assistant, staff_store, staff_tools
from bulmaai.web.core import BOT, Actor, Tier, audit, panel_guild, resolve_member, tier_for

log = logging.getLogger(__name__)

routes = web.RouteTableDef()

PATH = "/mcp"
VERSIONS = ("2025-06-18", "2025-03-26")
INSTRUCTIONS = (
    "DragonMineZ staff tools. Lookups run at once. Moderation and ticket actions are only proposed: the staff member "
    "confirms each one with a button Bulma DMs them on Discord."
)
ACTION_NOTE = " Only proposes the action; the user confirms it in Discord DMs."
RATE_LIMIT = 60
RATE_WINDOW = 60.0

_calls: dict[int, deque] = defaultdict(deque)


def rate_limited(user_id: int, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    window = _calls[user_id]
    while window and now - window[0] > RATE_WINDOW:
        window.popleft()
    if len(window) >= RATE_LIMIT:
        return True
    window.append(now)
    return False


def _unauthorized() -> web.Response:
    return web.json_response({"error": "Unauthorized."}, status=401, headers={"WWW-Authenticate": "Bearer"})


def _rpc_error(request_id: Any, code: int, message: str) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _rpc_result(request_id: Any, result: dict) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": result})


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


async def _authenticate(request: web.Request) -> tuple[Any, Tier, dict] | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    bot = request.app[BOT]
    if not bot.settings.staff_ai_enabled:
        return None
    user_id = await staff_store.user_for_token(token)
    if not user_id:
        return None
    guild = panel_guild(bot)
    if guild is None:
        return None
    member = await resolve_member(guild, user_id)
    if member is None:
        return None
    tier = tier_for(member, bot.settings)
    if tier < Tier.HELPER:
        return None
    profile = await staff_store.get_profile(user_id)
    if profile is None:
        return None
    return member, tier, profile


def _tool_list(tier: Tier) -> list[dict]:
    tools = []
    for schema in staff_tools.tools_for(tier):
        description = schema["description"]
        if schema["name"] in staff_tools.ACTION_TOOLS:
            description += ACTION_NOTE
        tools.append({"name": schema["name"], "description": description, "inputSchema": schema["input_schema"]})
    return tools


async def _call_tool(bot, member, tier: Tier, profile: dict, params: dict) -> dict:
    name = params["name"]
    arguments = params.get("arguments") or {}
    ctx = staff_tools.ToolContext(
        bot=bot,
        settings=bot.settings,
        guild=panel_guild(bot),
        member=member,
        tier=tier,
        origin="mcp",
        channel_id=None,
        profile=profile,
        language=staff_assistant.language_for(member, profile, bot.settings),
    )
    result = await staff_tools.run_tool(name, arguments, ctx)
    action_id = result.get("pending_action")
    if action_id is None:
        return _text(json.dumps(result, ensure_ascii=False, default=str), error="error" in result)
    summary = result.get("summary", "")
    cog = bot.get_cog("StaffAssistantCog")
    sent = False
    if cog is not None:
        try:
            sent = await cog.send_confirm_dm(member, action_id)
        except Exception:
            log.info("MCP confirm DM failed", exc_info=True)
    if not sent:
        await staff_store.cancel_action(action_id, member.id)
        return _text("Couldn't DM you; open your DMs to Bulma and try again.", error=True)
    target = arguments.get("user") or arguments.get("channel") or arguments.get("ticket")
    await audit(
        Actor(member, tier), "mcp.propose", target=str(target) if target is not None else None,
        tool=name, summary=summary,
    )
    return _text(f"Proposed: {summary}. Confirm it with the button in your Discord DMs (expires in 15 minutes).")


@routes.post(PATH)
async def post_mcp(request: web.Request) -> web.Response:
    auth = await _authenticate(request)
    if auth is None:
        return _unauthorized()
    member, tier, profile = auth
    try:
        message = json.loads(await request.read())
    except (ValueError, UnicodeDecodeError):
        return _rpc_error(None, -32700, "Parse error")
    if isinstance(message, list):
        return web.json_response({"error": "Batches are not supported."}, status=400)
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "Invalid request")
    if "id" not in message:
        return web.Response(status=202)
    request_id = message["id"]
    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str):
        return _rpc_error(request_id, -32600, "Invalid request")
    if params is not None and not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid params")
    params = params or {}
    if rate_limited(member.id):
        return _rpc_error(request_id, -32000, "Too many requests")

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _rpc_result(request_id, {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "bulma", "version": "1"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": _tool_list(tier)})
    if method == "tools/call":
        arguments = params.get("arguments")
        if not isinstance(params.get("name"), str) or not (arguments is None or isinstance(arguments, dict)):
            return _rpc_error(request_id, -32602, "Invalid params")
        return _rpc_result(request_id, await _call_tool(request.app[BOT], member, tier, profile, params))
    return _rpc_error(request_id, -32601, "Method not found")


@routes.get(PATH)
@routes.delete(PATH)
async def not_allowed(request: web.Request) -> web.Response:
    return web.Response(status=405, headers={"Allow": "POST"})
