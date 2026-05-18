from copy import deepcopy
import logging
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from bulmaai.bot import BulmaAI

log = logging.getLogger(__name__)

ToolFunc = Callable[..., Any]

# Responses API tools format (no nested "function" key)
TOOLS_SCHEMAS: dict[str, dict] = {}

# Bind tool names to Python functions (lazy loaded to avoid import-time issues)
TOOLS_FUNCS: dict[str, ToolFunc] = {}


def _init_tools_funcs() -> None:
    return


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

