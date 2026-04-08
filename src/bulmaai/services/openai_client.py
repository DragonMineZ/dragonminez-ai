import asyncio
import hashlib
import importlib.resources as pkg_resources
import json
import logging
from typing import Any, Optional, TypedDict

from openai import AsyncOpenAI

from bulmaai.config import load_settings
from bulmaai.services.support_cache import (
    build_support_cache_key,
    fetch_cached_support_response,
    get_docs_version,
    store_cached_support_response,
)
from bulmaai.utils.language import detect_language_from_text
from bulmaai.utils import tools_registry

settings = load_settings()
client = AsyncOpenAI(api_key=settings.openai_key)
log = logging.getLogger(__name__)


class ConversationMessage(TypedDict, total=False):
    role: str
    content: str
    speaker_name: str
    speaker_id: str
    speaker_kind: str


class ToolCallResult(TypedDict):
    name: str
    arguments: dict[str, Any]
    output: Any


class AgentResult(TypedDict):
    reply: str
    language: str
    tool_results: list[ToolCallResult]
    suggested_close: bool


def get_schemas(enabled_tools: list[str]) -> list[dict]:
    return tools_registry.get_schemas(enabled_tools)


def _build_safety_identifier(user_id: int) -> str:
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]
    return f"discord-user-{digest}"


def _message_to_input_content(message: ConversationMessage) -> str:
    content = (message.get("content") or "").strip()
    if not content:
        return ""

    if message.get("role") == "assistant":
        return content

    speaker_kind = message.get("speaker_kind", "participant")
    speaker_name = message.get("speaker_name", "unknown")
    speaker_id = message.get("speaker_id", "unknown")
    return f"[{speaker_kind} {speaker_name} id={speaker_id}]\n{content}"


def _build_response_input(
    messages: list[ConversationMessage],
    user_id: int,
    channel_id: int,
) -> list[dict[str, str]]:
    response_input: list[dict[str, str]] = [
        {
            "role": "developer",
            "content": f"Conversation meta: triggering_user_id={user_id}, channel_id={channel_id}",
        },
    ]

    for message in messages:
        content = _message_to_input_content(message)
        if not content:
            continue
        response_input.append(
            {
                "role": message.get("role", "user"),
                "content": content,
            }
        )
    return response_input


def _append_tool_output(
    response_input: list[dict[str, str]],
    *,
    name: str,
    output: Any,
) -> None:
    response_input.append(
        {
            "role": "developer",
            "content": f"Tool {name} output (JSON):\n{json.dumps(output, ensure_ascii=False)}",
        }
    )


def _latest_user_message(
    messages: list[ConversationMessage],
    *,
    target_speaker_id: str | None = None,
) -> ConversationMessage | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        if message.get("speaker_kind") == "staff":
            continue
        if target_speaker_id and message.get("speaker_id") != target_speaker_id:
            continue
        return message
    if target_speaker_id is not None:
        return _latest_user_message(messages)
    return None


def _build_docs_search_query(
    messages: list[ConversationMessage],
    *,
    target_speaker_id: str | None = None,
) -> str:
    latest_user = _latest_user_message(messages, target_speaker_id=target_speaker_id)
    if latest_user is None:
        return "support"

    resolved_speaker_id = target_speaker_id or latest_user.get("speaker_id")
    relevant_parts: list[str] = []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        if message.get("speaker_kind") == "staff":
            continue
        if resolved_speaker_id and message.get("speaker_id") != resolved_speaker_id:
            if relevant_parts:
                break
            continue
        content = (message.get("content") or "").strip()
        if content:
            relevant_parts.append(content)

    relevant_parts.reverse()
    query = "\n".join(relevant_parts).strip()
    return (query[:1500] or (latest_user.get("content") or "").strip() or "support")


def _load_system_prompt(lang: str) -> str:
    lang_code = lang if lang in {"en", "es", "pt"} else "en"
    filename = f"support_system_{lang_code}.txt"
    try:
        with pkg_resources.files("bulmaai.configs.prompts").joinpath(filename).open(
            "r", encoding="utf-8"
        ) as handle:
            return handle.read()
    except FileNotFoundError:
        return (
            "You are DragonMineZ's support assistant. Answer only from provided docs/tool outputs. "
            "If confidence is low, escalate to staff. Reply in the user's language."
        )


def _extract_function_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        calls.append(
            {
                "name": item.name,
                "arguments": getattr(item, "arguments", "{}"),
            }
        )
    return calls


def _extract_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text.strip()

    reply_text = ""
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) == "output_text":
                reply_text += getattr(part, "text", "")
    return reply_text.strip()


def _hydrate_tool_args(
    *,
    name: str,
    args: dict[str, Any],
    lang: str,
    user_id: int,
    channel_id: int,
) -> dict[str, Any]:
    hydrated = dict(args)
    if name == "docs_search":
        hydrated.setdefault("language", lang)
    elif name == "start_patreon_whitelist_flow":
        hydrated.setdefault("discord_user_id", str(user_id))
        hydrated.setdefault("ticket_channel_id", str(channel_id))
        hydrated.setdefault("nickname", None)
    return hydrated


async def _create_response(**kwargs: Any) -> Any:
    timeout_seconds = settings.ai_support_timeout_seconds
    return await asyncio.wait_for(
        client.responses.create(**kwargs),
        timeout=timeout_seconds,
    )


async def run_support_agent(
    *,
    messages: list[ConversationMessage],
    enabled_tools: list[str],
    language_hint: Optional[str] = None,
    model_override: Optional[str] = None,
    use_cache: bool = True,
    user_id: int,
    channel_id: int,
    bot: Any = None,
) -> AgentResult:
    model = model_override or settings.openai_support_model or settings.openai_model
    target_speaker_id = str(user_id)
    last_user = _latest_user_message(messages, target_speaker_id=target_speaker_id)
    if language_hint:
        language = language_hint
    elif last_user:
        language = detect_language_from_text(last_user["content"])
    else:
        language = "en"

    docs_version = None
    cache_key: str | None = None
    if use_cache:
        docs_version = await get_docs_version()
        cache_key = build_support_cache_key(
            messages=messages,
            enabled_tools=enabled_tools,
            language=language,
            channel_id=channel_id,
        )
        cached = await fetch_cached_support_response(cache_key, docs_version)
        if cached is not None:
            log.info("support_cache hit channel=%s language=%s", channel_id, language)
            return AgentResult(**cached)

    system_prompt = _load_system_prompt(language)
    response_input = _build_response_input(messages, user_id=user_id, channel_id=channel_id)
    tool_results: list[ToolCallResult] = []
    remaining_tools = list(enabled_tools)

    if "docs_search" in remaining_tools:
        docs_query = _build_docs_search_query(messages, target_speaker_id=target_speaker_id)
        docs_output = await tools_registry.get_func("docs_search")(query=docs_query, language=language)
        tool_results.append(
            ToolCallResult(
                name="docs_search",
                arguments={"query": docs_query, "language": language},
                output=docs_output,
            )
        )
        _append_tool_output(response_input, name="docs_search", output=docs_output)
        remaining_tools = [tool_name for tool_name in remaining_tools if tool_name != "docs_search"]

    tools = get_schemas(remaining_tools)

    request_kwargs: dict[str, Any] = {
        "model": model,
        "instructions": system_prompt,
        "input": response_input,
        "max_output_tokens": settings.openai_support_max_output_tokens,
        "prompt_cache_key": f"support:{channel_id}:{language}",
        "safety_identifier": _build_safety_identifier(user_id),
        "text": {"verbosity": "medium"},
    }
    if tools:
        request_kwargs["tools"] = tools
        request_kwargs["tool_choice"] = "auto"
    if model.startswith("gpt-5"):
        request_kwargs["reasoning"] = {
            "effort": settings.openai_support_reasoning_effort,
            "summary": "auto",
        }

    response = await _create_response(**request_kwargs)
    result = await _handle_tools_and_final_reply(
        response=response,
        base_input=response_input,
        base_tool_results=tool_results,
        system_prompt=system_prompt,
        model=model,
        lang=language,
        bot=bot,
        user_id=user_id,
        channel_id=channel_id,
    )
    if use_cache and cache_key is not None:
        await store_cached_support_response(cache_key, docs_version, dict(result))
    return result


async def _handle_tools_and_final_reply(
    *,
    response: Any,
    base_input: list[dict[str, str]],
    base_tool_results: list[ToolCallResult] | None,
    system_prompt: str,
    model: str,
    lang: str,
    user_id: int,
    channel_id: int,
    bot: Any = None,
) -> AgentResult:
    tool_results: list[ToolCallResult] = list(base_tool_results or [])
    function_calls = _extract_function_calls(response)

    if function_calls:
        followup_input = list(base_input)
        for call in function_calls:
            name = call["name"]
            raw_args = call["arguments"]
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}

            args = _hydrate_tool_args(
                name=name,
                args=args,
                lang=lang,
                user_id=user_id,
                channel_id=channel_id,
            )

            func = tools_registry.get_func(name, bot_context=bot)
            output = await func(**args)
            tool_results.append(ToolCallResult(name=name, arguments=args, output=output))
            _append_tool_output(followup_input, name=name, output=output)

        followup_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": system_prompt,
            "input": followup_input,
            "max_output_tokens": settings.openai_support_max_output_tokens,
            "prompt_cache_key": f"support:{channel_id}:{lang}:post-tool",
            "safety_identifier": _build_safety_identifier(user_id),
            "text": {"verbosity": "medium"},
        }
        if model.startswith("gpt-5"):
            followup_kwargs["reasoning"] = {
                "effort": settings.openai_support_reasoning_effort,
                "summary": "auto",
            }
        response = await _create_response(**followup_kwargs)

    reply_text = _extract_output_text(response) or "(no reply)"
    lowered = reply_text.lower()
    suggested_close = any(
        phrase in lowered
        for phrase in [
            "ticket can be closed",
            "puede cerrarse el ticket",
            "pode ser fechado",
        ]
    )

    return AgentResult(
        reply=reply_text,
        language=lang,
        tool_results=tool_results,
        suggested_close=suggested_close,
    )
