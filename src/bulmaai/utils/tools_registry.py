import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

ToolFunc = Callable[..., Any]

# Responses API tools format (no nested "function" key)
TOOLS_SCHEMAS: dict[str, dict] = {
    "docs_search": {
        "type": "function",
        "name": "docs_search",
        "description": (
            "Search DragonMineZ documentation for relevant information "
            "to answer the user's question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "User query or question to search for.",
                },
                "language": {
                    "type": "string",
                    "enum": ["en", "es", "pt"],
                    "description": (
                        "Language of the user's question. "
                        "Docs may be available in 'en' and 'es'; "
                        "if 'pt', use the closest language."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "Maximum number of doc snippets to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "start_patreon_whitelist_flow": {
        "type": "function",
        "name": "start_patreon_whitelist_flow",
        "description": (
            "Start the Patreon whitelist workflow for a Discord user. "
            "Use when a Patreon asks how to get beta access / whitelist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "discord_user_id": {
                    "type": "string",
                    "description": "Discord user ID of the requester.",
                },
                "ticket_channel_id": {
                    "type": "string",
                    "description": (
                        "ID of the Discord ticket channel where the request is happening."
                    ),
                },
            },
            "required": ["discord_user_id", "ticket_channel_id"],
            "additionalProperties": False,
        },
    },
}

# Bind tool names to Python functions (lazy loaded to avoid import-time issues)
TOOLS_FUNCS: dict[str, ToolFunc] = {}


def _init_tools_funcs() -> None:
    """Lazily import and initialize tool functions."""
    global TOOLS_FUNCS
    if TOOLS_FUNCS:  # Already initialized
        return

    from bulmaai.utils import docs_search, patreon_whitelist
    from bulmaai.bot import BulmaAI

    bot = BulmaAI.instance
    if bot is None:
        raise RuntimeError("Bot instance not initialized when trying to set up tools")

    # Get the Cog instance from the bot
    patreon_cog = bot.get_cog("PatreonWhitelistTool")
    if patreon_cog is None:
        raise RuntimeError("PatreonWhitelistTool cog not loaded")

    TOOLS_FUNCS = {
        "docs_search": docs_search.run_docs_search,
        "start_patreon_whitelist_flow": patreon_cog.start_patreon_whitelist_flow,
    }

    log.info(f"✅ Tools initialized: {list(TOOLS_FUNCS.keys())}")


def get_schemas(enabled_tools: list[str]) -> list[dict]:
    """
    Return tool schemas for the given tool names, in Responses API format.
    """
    return [TOOLS_SCHEMAS[name] for name in enabled_tools if name in TOOLS_SCHEMAS]


def get_func(name: str) -> ToolFunc:
    """
    Return the Python function for a given tool name.
    Raises KeyError if unknown.
    """
    _init_tools_funcs()
    return TOOLS_FUNCS[name]

