import asyncio
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
from bulmaai.utils import tools_registry

settings = load_settings()
client = AsyncOpenAI(api_key=settings.openai_key)
log = logging.getLogger(__name__)


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


def _detect_language_from_text(text: str) -> str:
    if not text or len(text.strip()) < 3:
        return "en"

    text_lower = text.lower()
    words = set(text_lower.split())

    pt_markers = {
        "não", "você", "está", "são", "também", "muito", "porque", "obrigado",
        "obrigada", "então", "isso", "assim", "aqui", "ainda", "pode", "fazer",
        "tenho", "meu", "minha", "seu", "sua", "como", "quando", "onde", "qual",
        "oi", "olá", "tudo", "bom", "boa", "dia", "noite", "tarde", "por", "favor",
        "ajuda", "preciso", "quero", "problema", "funciona", "funcionando", "erro",
        "jogo", "servidor", "baixar", "instalar", "versão", "atualização",
    }
    es_markers = {
        "no", "está", "son", "también", "mucho", "porque", "gracias", "entonces",
        "esto", "así", "aquí", "todavía", "puede", "hacer", "tengo", "mi", "tu",
        "su", "como", "cuando", "donde", "cual", "cuál", "hola", "todo", "buen",
        "buena", "día", "noche", "tarde", "por", "favor", "ayuda", "necesito",
        "quiero", "problema", "funciona", "funcionando", "error", "juego",
        "servidor", "descargar", "instalar", "versión", "actualización", "qué",
    }
    en_markers = {
        "the", "is", "are", "was", "were", "have", "has", "been", "being", "do",
        "does", "did", "will", "would", "could", "should", "can", "may", "might",
        "must", "shall", "this", "that", "these", "those", "what", "which", "who",
        "how", "why", "when", "where", "hello", "hi", "thanks", "thank", "please",
        "help", "need", "want", "problem", "issue", "work", "working", "error",
        "game", "server", "download", "install", "version", "update", "crash",
    }

    pt_score = len(words & pt_markers)
    es_score = len(words & es_markers)
    en_score = len(words & en_markers)

    if any(char in text_lower for char in "çãõáéíóúâêôà"):
        pt_score += 4
    if any(char in text_lower for char in "ñ¿¡"):
        es_score += 4

    if pt_score > es_score and pt_score > en_score:
        return "pt"
    if es_score > en_score and es_score > pt_score:
        return "es"
    return "en"


def _collapse_history_to_text(messages: list[dict[str, str]], user_id: int, channel_id: int) -> str:
    parts = [
        f"Conversation meta: discord_user_id={user_id}, ticket_channel_id={channel_id}",
    ]
    for message in messages:
        role = message.get("role", "user")
        prefix = "User:" if role == "user" else "Assistant:"
        parts.append(f"{prefix} {message.get('content', '')}")
    return "\n".join(parts)


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
    messages: list[dict[str, str]],
    enabled_tools: list[str],
    language_hint: Optional[str] = None,
    user_id: int,
    channel_id: int,
    bot: Any = None,
) -> AgentResult:
    model = settings.openai_support_model or settings.openai_model
    last_user = next((message for message in reversed(messages) if message.get("role") == "user"), None)
    if language_hint:
        language = language_hint
    elif last_user:
        language = _detect_language_from_text(last_user["content"])
    else:
        language = "en"

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
    transcript = _collapse_history_to_text(messages, user_id=user_id, channel_id=channel_id)
    tools = get_schemas(enabled_tools)

    request_kwargs: dict[str, Any] = {
        "model": model,
        "instructions": system_prompt,
        "input": transcript,
        "max_output_tokens": settings.openai_support_max_output_tokens,
        "prompt_cache_key": f"support:{channel_id}:{language}",
        "safety_identifier": f"discord-user-{user_id}",
        "text": {"verbosity": "low"},
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
        base_transcript=transcript,
        system_prompt=system_prompt,
        model=model,
        lang=language,
        bot=bot,
        user_id=user_id,
        channel_id=channel_id,
    )
    await store_cached_support_response(cache_key, docs_version, dict(result))
    return result


async def _handle_tools_and_final_reply(
    *,
    response: Any,
    base_transcript: str,
    system_prompt: str,
    model: str,
    lang: str,
    user_id: int,
    channel_id: int,
    bot: Any = None,
) -> AgentResult:
    tool_results: list[ToolCallResult] = []
    transcript = base_transcript
    function_calls = _extract_function_calls(response)

    if function_calls:
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
            transcript += f"\nTool {name} output:\n{json.dumps(output, ensure_ascii=False)}\n"

        followup_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": system_prompt,
            "input": transcript,
            "max_output_tokens": settings.openai_support_max_output_tokens,
            "prompt_cache_key": f"support:{channel_id}:{lang}:post-tool",
            "safety_identifier": f"discord-user-{user_id}",
            "text": {"verbosity": "low"},
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
