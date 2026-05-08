import asyncio
import hashlib
import importlib.resources as pkg_resources
import json
import logging
import re
import time
from typing import Any, Optional, TypedDict

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from bulmaai.config import Settings, load_settings
from bulmaai.services.support_traces import (
    SupportAITrace,
    get_support_session,
    record_support_ai_trace,
    upsert_support_session,
)
from bulmaai.utils.language import detect_language_from_text
from bulmaai.utils import tools_registry

client = AsyncOpenAI(api_key=load_settings().openai_key)
log = logging.getLogger(__name__)
MINECRAFT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
NICKNAME_HINT_RE = re.compile(
    r"(?i)\b(?:ign|in[- ]?game name|minecraft(?: username| name)?|mc(?: username| name)?|nickname|username|name)"
    r"\s*(?:is|:|=)?\s*([A-Za-z0-9_]{3,16})\b"
)
PATREON_WHITELIST_KEYWORDS = (
    "patreon whitelist",
    "patreon allowlist",
    "patreon beta",
    "patreon access",
    "patreon-only",
    "whitelist access",
    "allowlist access",
    "beta whitelist",
    "beta access",
)

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


def _latest_non_staff_user_text(messages: list[ConversationMessage]) -> str:
    latest = _latest_user_message(messages)
    return (latest.get("content") if latest else "") or ""


def _looks_like_patreon_whitelist_request(messages: list[ConversationMessage]) -> bool:
    text = _latest_non_staff_user_text(messages).lower()
    if not text:
        return False
    if any(keyword in text for keyword in PATREON_WHITELIST_KEYWORDS):
        return True
    return ("whitelist" in text or "allowlist" in text) and ("patreon" in text or "beta" in text)


def _extract_minecraft_nickname_guess(messages: list[ConversationMessage]) -> str | None:
    text = _latest_non_staff_user_text(messages)
    match = NICKNAME_HINT_RE.search(text)
    if not match:
        return None
    nickname = match.group(1).strip()
    return nickname if MINECRAFT_NAME_RE.match(nickname) else None


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
    if name == "start_patreon_whitelist_flow":
        hydrated.setdefault("discord_user_id", str(user_id))
        hydrated.setdefault("ticket_channel_id", str(channel_id))
        hydrated.setdefault("nickname", None)
    return hydrated


def _select_reasoning_effort(settings: Any, *, high_confidence: bool = False) -> str:
    default_effort = getattr(settings, "openai_support_reasoning_effort", "medium")
    fast_effort = getattr(settings, "openai_support_fast_reasoning_effort", default_effort)
    if high_confidence:
        return fast_effort
    return default_effort


def _build_file_search_tool(settings: Any) -> dict[str, Any] | None:
    vector_store_ids = [
        str(value).strip()
        for value in getattr(settings, "openai_support_vector_store_ids", ())
        if str(value).strip()
    ]
    if not vector_store_ids:
        return None
    tool: dict[str, Any] = {
        "type": "file_search",
        "vector_store_ids": vector_store_ids,
    }
    try:
        max_results = int(getattr(settings, "openai_support_file_search_max_results", 0) or 0)
    except (TypeError, ValueError):
        max_results = 0
    if max_results > 0:
        tool["max_num_results"] = max_results
    return tool


def _build_prompt_cache_key(*, model: str, language: str, tools: list[dict[str, Any]]) -> str:
    tool_signature = hashlib.sha256(
        json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return f"support:{model}:{language}:{tool_signature}"


async def _run_direct_patreon_whitelist_flow(
    *,
    messages: list[ConversationMessage],
    language: str,
    user_id: int,
    channel_id: int,
    bot: Any,
) -> AgentResult:
    args = {
        "discord_user_id": str(user_id),
        "ticket_channel_id": str(channel_id),
        "nickname": _extract_minecraft_nickname_guess(messages),
    }
    func = tools_registry.get_func("start_patreon_whitelist_flow", bot_context=bot)
    output = await func(**args)
    tool_results = [
        ToolCallResult(
            name="start_patreon_whitelist_flow",
            arguments=args,
            output=output,
        )
    ]
    reply = "(no reply)"
    if not (isinstance(output, dict) and output.get("suppress_ai_reply") is True):
        reply = str(output.get("message") if isinstance(output, dict) else output)
    return AgentResult(
        reply=reply,
        language=language,
        tool_results=tool_results,
        suggested_close=False,
    )


async def _create_response(*, timeout_seconds: int, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        client.responses.create(**kwargs),
        timeout=timeout_seconds,
    )


async def _create_conversation() -> Any:
    return await client.conversations.create()


def is_transient_ai_error(error: BaseException) -> bool:
    if isinstance(error, (asyncio.TimeoutError, APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True
    if isinstance(error, APIStatusError):
        status_code = getattr(error, "status_code", None)
        return status_code in {408, 409, 425, 429} or bool(
            isinstance(status_code, int) and status_code >= 500
        )
    return False


def _build_openai_metadata(
    *,
    workflow: str,
    language: str,
    channel_id: int,
    user_id: int,
    file_search_enabled: bool,
    ticket_conversation: bool,
) -> dict[str, str]:
    return {
        "app": "dragonminez-ai",
        "workflow": workflow,
        "language": language,
        "discord_channel_id": str(channel_id),
        "discord_user": _build_safety_identifier(user_id),
        "file_search": str(file_search_enabled).lower(),
        "ticket_conversation": str(ticket_conversation).lower(),
    }


def _get_attr_or_key(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _usage_int(usage: Any, *names: str) -> int | None:
    for name in names:
        value = _get_attr_or_key(usage, name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_response_usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    input_details = (
        _get_attr_or_key(usage, "input_tokens_details")
        or _get_attr_or_key(usage, "prompt_tokens_details")
    )
    output_details = (
        _get_attr_or_key(usage, "output_tokens_details")
        or _get_attr_or_key(usage, "completion_tokens_details")
    )
    return {
        "input_tokens": _usage_int(usage, "input_tokens", "prompt_tokens"),
        "output_tokens": _usage_int(usage, "output_tokens", "completion_tokens"),
        "total_tokens": _usage_int(usage, "total_tokens"),
        "cached_tokens": _usage_int(input_details, "cached_tokens"),
        "reasoning_tokens": _usage_int(output_details, "reasoning_tokens"),
    }


def _tool_names_for_trace(tools: list[dict[str, Any]], tool_results: list[ToolCallResult]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        name = str(tool.get("name") or tool.get("type") or "").strip()
        if name:
            names.append(name)
    for result in tool_results:
        name = result.get("name")
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


async def run_support_agent(
    *,
    messages: list[ConversationMessage],
    enabled_tools: list[str],
    language_hint: Optional[str] = None,
    model_override: Optional[str] = None,
    user_id: int,
    channel_id: int,
    ticket_conversation: bool = False,
    bot: Any = None,
    settings: Settings | None = None,
) -> AgentResult:
    runtime_settings = settings or load_settings()
    model = model_override or runtime_settings.openai_support_model or runtime_settings.openai_model
    target_speaker_id = str(user_id)
    last_user = _latest_user_message(messages, target_speaker_id=target_speaker_id)
    if language_hint:
        language = language_hint
    elif last_user:
        language = detect_language_from_text(last_user["content"])
    else:
        language = "en"

    if (
        "start_patreon_whitelist_flow" in enabled_tools
        and _looks_like_patreon_whitelist_request(messages)
    ):
        return await _run_direct_patreon_whitelist_flow(
            messages=messages,
            language=language,
            user_id=user_id,
            channel_id=channel_id,
            bot=bot,
        )

    openai_conversation_id: str | None = None
    conversation_already_exists = False
    if ticket_conversation:
        try:
            session = await get_support_session(channel_id)
            if session is None:
                conversation = await _create_conversation()
                openai_conversation_id = getattr(conversation, "id", None)
            else:
                conversation_already_exists = True
                openai_conversation_id = session.openai_conversation_id
        except Exception:
            log.exception(
                "Failed to resolve OpenAI support conversation",
                extra={"event": "support_conversation_resolve_failed", "channel_id": channel_id},
            )
            openai_conversation_id = None

    input_messages = messages
    if conversation_already_exists and last_user is not None:
        input_messages = [last_user]

    system_prompt = _load_system_prompt(language)
    response_input = _build_response_input(input_messages, user_id=user_id, channel_id=channel_id)
    tool_results: list[ToolCallResult] = []
    tools = get_schemas(enabled_tools)
    file_search_tool = _build_file_search_tool(runtime_settings)
    if file_search_tool is not None:
        tools.append(file_search_tool)
    file_search_enabled = file_search_tool is not None
    vector_store_ids = list(file_search_tool.get("vector_store_ids", [])) if file_search_tool else []
    prompt_cache_key = _build_prompt_cache_key(model=model, language=language, tools=tools)
    request_metadata = _build_openai_metadata(
        workflow="support_question",
        language=language,
        channel_id=channel_id,
        user_id=user_id,
        file_search_enabled=file_search_enabled,
        ticket_conversation=ticket_conversation,
    )

    request_kwargs: dict[str, Any] = {
        "model": model,
        "instructions": system_prompt,
        "input": response_input,
        "max_output_tokens": runtime_settings.openai_support_max_output_tokens,
        "metadata": request_metadata,
        "prompt_cache_key": prompt_cache_key,
        "safety_identifier": _build_safety_identifier(user_id),
        "store": bool(getattr(runtime_settings, "openai_support_store_responses", True)),
        "text": {"verbosity": "medium"},
    }
    if openai_conversation_id:
        request_kwargs["conversation"] = openai_conversation_id
    if tools:
        request_kwargs["tools"] = tools
        request_kwargs["tool_choice"] = "auto"
    if model.startswith("gpt-5"):
        request_kwargs["reasoning"] = {
            "effort": _select_reasoning_effort(
                runtime_settings,
                high_confidence=file_search_enabled,
            ),
            "summary": "auto",
        }

    started_at = time.perf_counter()
    response = await _create_response(
        timeout_seconds=runtime_settings.ai_support_timeout_seconds,
        **request_kwargs,
    )
    result = await _handle_tools_and_final_reply(
        response=response,
        base_input=response_input,
        base_tool_results=tool_results,
        system_prompt=system_prompt,
        model=model,
        lang=language,
        settings=runtime_settings,
        bot=bot,
        user_id=user_id,
        channel_id=channel_id,
        trace_context={
            "started_at": started_at,
            "workflow": "support_question",
            "model": model,
            "prompt_cache_key": prompt_cache_key,
            "request_metadata": request_metadata,
            "input_json": response_input,
            "tools": tools,
            "file_search_enabled": file_search_enabled,
            "vector_store_ids": vector_store_ids,
            "openai_conversation_id": openai_conversation_id,
            "previous_response_id": None,
        },
    )
    if openai_conversation_id:
        try:
            await upsert_support_session(
                channel_id=channel_id,
                openai_conversation_id=openai_conversation_id,
                last_response_id=result.get("response_id"),
            )
        except Exception:
            log.exception(
                "Failed to update OpenAI support conversation",
                extra={
                    "event": "support_conversation_update_failed",
                    "channel_id": channel_id,
                    "openai_conversation_id": openai_conversation_id,
                },
            )
    return result


async def _handle_tools_and_final_reply(
    *,
    response: Any,
    base_input: list[dict[str, str]],
    base_tool_results: list[ToolCallResult] | None,
    system_prompt: str,
    model: str,
    lang: str,
    settings: Settings,
    user_id: int,
    channel_id: int,
    bot: Any = None,
    trace_context: dict[str, Any] | None = None,
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

        if any(
            isinstance(result.get("output"), dict)
            and result["output"].get("suppress_ai_reply") is True
            for result in tool_results
        ):
            return AgentResult(
                reply="(no reply)",
                language=lang,
                tool_results=tool_results,
                suggested_close=False,
            )

        followup_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": system_prompt,
            "input": followup_input,
            "max_output_tokens": settings.openai_support_max_output_tokens,
            "metadata": _build_openai_metadata(
                workflow="support_tool_followup",
                language=lang,
                channel_id=channel_id,
                user_id=user_id,
                file_search_enabled=bool((trace_context or {}).get("file_search_enabled")),
                ticket_conversation=bool((trace_context or {}).get("openai_conversation_id")),
            ),
            "prompt_cache_key": f"support:{model}:{lang}:post-tool",
            "safety_identifier": _build_safety_identifier(user_id),
            "store": bool(getattr(settings, "openai_support_store_responses", True)),
            "text": {"verbosity": "medium"},
        }
        openai_conversation_id = (trace_context or {}).get("openai_conversation_id")
        if openai_conversation_id:
            followup_kwargs["conversation"] = openai_conversation_id
        if model.startswith("gpt-5"):
            followup_kwargs["reasoning"] = {
                "effort": settings.openai_support_reasoning_effort,
                "summary": "auto",
            }
        response = await _create_response(
            timeout_seconds=settings.ai_support_timeout_seconds,
            **followup_kwargs,
        )

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

    response_id = getattr(response, "id", None)
    if trace_context is not None:
        usage = _extract_response_usage(response)
        latency_ms = int((time.perf_counter() - trace_context["started_at"]) * 1000)
        try:
            await record_support_ai_trace(
                SupportAITrace(
                    workflow=str(trace_context["workflow"]),
                    response_id=response_id,
                    openai_conversation_id=trace_context.get("openai_conversation_id"),
                    previous_response_id=trace_context.get("previous_response_id"),
                    model=str(trace_context["model"]),
                    language=lang,
                    channel_id=channel_id,
                    user_id=user_id,
                    prompt_cache_key=trace_context.get("prompt_cache_key"),
                    file_search_enabled=bool(trace_context.get("file_search_enabled")),
                    vector_store_ids=list(trace_context.get("vector_store_ids") or []),
                    tool_names=_tool_names_for_trace(
                        list(trace_context.get("tools") or []),
                        tool_results,
                    ),
                    latency_ms=latency_ms,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    total_tokens=usage["total_tokens"],
                    cached_tokens=usage["cached_tokens"],
                    reasoning_tokens=usage["reasoning_tokens"],
                    reply_text=reply_text,
                    input_json=trace_context.get("input_json") or [],
                    request_metadata=trace_context.get("request_metadata") or {},
                )
            )
        except Exception:
            log.exception(
                "Failed to record OpenAI support trace",
                extra={"event": "support_trace_record_failed", "channel_id": channel_id},
            )

    result = AgentResult(
        reply=reply_text,
        language=lang,
        tool_results=tool_results,
        suggested_close=suggested_close,
    )
    if response_id:
        result["response_id"] = response_id
    return result
