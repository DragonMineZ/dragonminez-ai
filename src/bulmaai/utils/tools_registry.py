from copy import deepcopy
import logging
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from bulmaai.bot import BulmaAI

from bulmaai.services import ai_tools

log = logging.getLogger(__name__)

ToolFunc = Callable[..., Any]

# Source schemas: {"type": "function", "name", "description", "parameters", "strict"}
TOOLS_SCHEMAS: dict[str, dict] = dict(ai_tools.TOOL_SCHEMAS)

TOOLS_FUNCS: dict[str, ToolFunc] = dict(ai_tools.TOOL_FUNCS)


def _normalize_schema(name: str) -> dict[str, Any]:
    source = TOOLS_SCHEMAS[name]
    input_schema = deepcopy(source.get("parameters") or {"type": "object", "properties": {}})
    strict = source.get("strict") is True
    if strict:
        properties = input_schema.get("properties")
        if isinstance(properties, dict):
            input_schema["required"] = list(properties.keys())
    schema: dict[str, Any] = {
        "name": source.get("name", name),
        "description": source.get("description", ""),
        "input_schema": input_schema,
    }
    if strict:
        schema["strict"] = True
    return schema


def get_schemas(enabled_tools: list[str]) -> list[dict]:
    """
    Return tool schemas for the given tool names, in Claude Messages API format
    ({"name", "description", "input_schema"}, plus "strict" when the source had it).
    """
    return [_normalize_schema(name) for name in enabled_tools if name in TOOLS_SCHEMAS]


def get_func(name: str, bot_context: "BulmaAI | None" = None) -> ToolFunc:
    """
    Return the Python function for a given tool name.
    If bot_context is provided, returns a wrapper that injects it.
    Raises KeyError if unknown.
    """
    func = TOOLS_FUNCS[name]

    # If bot_context is provided, wrap the function to inject it
    if bot_context is not None:
        async def wrapper(**kwargs):
            # Inject _bot_context parameter
            return await func(**kwargs, _bot_context=bot_context)
        return wrapper

    return func

