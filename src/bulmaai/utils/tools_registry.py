from copy import deepcopy
import logging
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from bulmaai.bot import BulmaAI

log = logging.getLogger(__name__)

ToolFunc = Callable[..., Any]

# Responses API tools format (no nested "function" key)
TOOLS_SCHEMAS: dict[str, dict] = {
    "docs_search": {
        "type": "function",
        "name": "docs_search",
        "description": (
            "Search DragonMineZ documentation and indexed resolved tickets "
            "for relevant information to answer the user's question."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "User query or question to search for.",
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
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "nickname": {
                    "type": ["string", "null"],
                    "description": (
                        "Minecraft nickname if the user already provided it in the "
                        "conversation. Use null if it is unknown."
                    ),
                },
            },
            "required": ["nickname"],
            "additionalProperties": False,
        },
    },
}

# Bind tool names to Python functions (lazy loaded to avoid import-time issues)
TOOLS_FUNCS: dict[str, ToolFunc] = {}


def _init_tools_funcs() -> None:
    global TOOLS_FUNCS
    if TOOLS_FUNCS:  # Already initialized
        return

    # Lazy import to avoid circular imports
    from bulmaai.utils import docs_search, patreon_whitelist

    TOOLS_FUNCS = {
        "docs_search": docs_search.run_docs_search,
        "start_patreon_whitelist_flow": patreon_whitelist.start_patreon_whitelist_flow,
    }


def _normalize_schema(name: str) -> dict[str, Any]:
    schema = deepcopy(TOOLS_SCHEMAS[name])
    parameters = schema.get("parameters")
    if schema.get("strict") is True and isinstance(parameters, dict):
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            parameters["required"] = list(properties.keys())
    return schema


def get_schemas(enabled_tools: list[str]) -> list[dict]:
    """
    Return tool schemas for the given tool names, in Responses API format.
    """
    return [_normalize_schema(name) for name in enabled_tools if name in TOOLS_SCHEMAS]


def get_func(name: str, bot_context: "BulmaAI | None" = None) -> ToolFunc:
    """
    Return the Python function for a given tool name.
    If bot_context is provided, returns a wrapper that injects it.
    Raises KeyError if unknown.
    """
    _init_tools_funcs()
    func = TOOLS_FUNCS[name]

    # If bot_context is provided, wrap the function to inject it
    if bot_context is not None:
        async def wrapper(**kwargs):
            # Inject _bot_context parameter
            return await func(**kwargs, _bot_context=bot_context)
        return wrapper

    return func

