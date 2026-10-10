import asyncio
import difflib
import functools
import hashlib
import importlib.resources as pkg_resources
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
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
from bulmaai.services import ai_budget, ai_guard, ai_tools
from bulmaai.services.support_traces import SupportAITrace, record_support_ai_trace
from bulmaai.services.wiki_knowledge import (
    DEFAULT_WIKI_BASE_URL,
    wiki_url_for_citation_filename,
)
from bulmaai.utils.language import detect_language_from_text
from bulmaai.utils import tools_registry

client = AsyncOpenAI(api_key=load_settings().openai_key)
log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3
MAX_KNOWLEDGE_CHUNK_CHARS = 1500
REPEAT_SIMILARITY = 0.85
REPEAT_MIN_CHARS = 40
REPEAT_NOTE = (
    "Your draft repeated one of your earlier replies in this transcript. The requester is still stuck: "
    "do not restate it. Give a genuinely different next step, ask one targeted question, or hand off to staff."
)
CLARIFY_LOOP_NOTE = (
    "You already asked the requester a clarifying question and they answered it. Do not ask another one: "
    "answer with what you have now, or hand off to staff."
)


class ConversationMessage(TypedDict, total=False):
    role: str
    content: str
    speaker_name: str
    speaker_id: str
    speaker_kind: str
    age: str
    reply_to: str


class ToolCallResult(TypedDict):
    name: str
    arguments: dict[str, Any]
    output: Any


class AgentResult(TypedDict, total=False):
    reply: str
    language: str
    tool_results: list[ToolCallResult]
    suggested_close: bool
    confidence: float | None
    kind: str | None
    model: str
    escalated: bool
    paused: bool
    response_id: str
    guard_flags: frozenset[str]


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

    # Names, quoted replies and message text are member-controlled; none of them may forge a speaker label.
    content = ai_guard.neutralize_labels(content)
    reply_to = " ".join(str(message.get("reply_to") or "").replace("[", "(").replace("]", ")").split())
    label = " · ".join(
        part
        for part in (
            f"{message.get('speaker_kind', 'participant')} {ai_guard.safe_name(message.get('speaker_name'))}",
            message.get("age"),
            f"replying to {reply_to}" if reply_to else None,
        )
        if part
    )
    return f"[{label}]\n{content}"


def _merge_assistant_turns(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """Long replies are sent as several Discord messages; the model should see them as one turn."""
    merged: list[ConversationMessage] = []
    for message in messages:
        if merged and message.get("role") == "assistant" and merged[-1].get("role") == "assistant":
            previous = merged[-1]
            merged[-1] = ConversationMessage(
                **{**previous, "content": f"{previous.get('content', '')}\n{message.get('content', '')}"}
            )
        else:
            merged.append(message)
    return merged


def _assistant_replies(messages: list[ConversationMessage]) -> list[str]:
    return [
        message.get("content", "")
        for message in _merge_assistant_turns(messages)
        if message.get("role") == "assistant"
    ]


def _build_response_input(
    messages: list[ConversationMessage],
    *,
    context_lines: list[str] | None = None,
) -> list[dict[str, str]]:
    merged = _merge_assistant_turns(messages)
    context = [
        *(context_lines or []),
        f"today (UTC): {datetime.now(timezone.utc).date().isoformat()}",
        f"your earlier replies in this transcript: {sum(1 for m in merged if m.get('role') == 'assistant')}",
    ]
    response_input: list[dict[str, str]] = [
        {"role": "developer", "content": "Context:\n" + "\n".join(f"- {line}" for line in context)},
    ]
    for message in merged:
        content = _message_to_input_content(message)
        if content:
            response_input.append({"role": message.get("role", "user"), "content": content})
    return response_input


def _build_data_input(knowledge: list[dict[str, str]], prefetch_lines: list[str]) -> list[dict[str, str]]:
    parts: list[str] = []
    if prefetch_lines:
        parts.append(
            "Looked-up data (live and authoritative; requester_* entries describe the requester only):\n"
            + "\n".join(prefetch_lines)
        )
    if knowledge:
        parts.append(
            "Knowledge search results for this conversation (cite the files you used in [sources: ...]):\n\n"
            + "\n\n".join(f"### {hit['filename']}\n{hit['text']}" for hit in knowledge)
        )
    return [{"role": "developer", "content": "\n\n".join(parts)}] if parts else []


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


def _knowledge_query(messages: list[ConversationMessage], user_id: int) -> str:
    """The requester's last two messages: follow-ups like "still broken" need the earlier one."""
    requester_turns = [
        message.get("content", "")
        for message in messages
        if message.get("role") == "user" and message.get("speaker_id") == str(user_id)
    ]
    if not requester_turns:
        latest = _latest_user_message(messages)
        requester_turns = [latest.get("content", "")] if latest else []
    return "\n".join(requester_turns[-2:])[-800:].strip()


@functools.lru_cache(maxsize=1)
def _load_system_prompt(lang: str = "en") -> str:
    parts: list[str] = []
    for filename in ("support_system_en.txt", "support_facts_en.txt"):
        try:
            with pkg_resources.files("bulmaai.configs.prompts").joinpath(filename).open(
                "r", encoding="utf-8"
            ) as handle:
                parts.append(handle.read().strip())
        except FileNotFoundError:
            continue
    return "\n\n".join(parts) or (
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
                "call_id": getattr(item, "call_id", None),
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


def _extract_file_citations(response: Any) -> list[str]:
    """Collect cited knowledge filenames from file_search annotations, in order."""
    filenames: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) != "output_text":
                continue
            for annotation in getattr(part, "annotations", []) or []:
                if getattr(annotation, "type", None) != "file_citation":
                    continue
                filename = getattr(annotation, "filename", None)
                if filename and filename not in filenames:
                    filenames.append(str(filename))
    return filenames


MAX_WIKI_SOURCE_LINKS = 3
CONFIDENCE_MARKER_RE = re.compile(r"\[\s*confidence\s*[:=]\s*([0-9]*\.?[0-9]+)\s*\]", re.IGNORECASE)
KIND_MARKER_RE = re.compile(r"\[\s*kind\s*[:=]\s*(answer|clarify|handoff|offtopic)\s*\]", re.IGNORECASE)
SOURCES_MARKER_RE = re.compile(r"\[\s*sources\s*[:=]\s*([^\]]*)\]", re.IGNORECASE)


def _split_confidence(reply_text: str) -> tuple[str, float | None]:
    """Strip every `[confidence: x]` marker; the last one wins, clamped to 0..1."""
    matches = CONFIDENCE_MARKER_RE.findall(reply_text)
    if not matches:
        return reply_text, None
    stripped = CONFIDENCE_MARKER_RE.sub("", reply_text).strip() or "(no reply)"
    return stripped, max(0.0, min(float(matches[-1]), 1.0))


def _split_kind(reply_text: str) -> tuple[str, str | None]:
    """Strip every `[kind: x]` marker; the last one wins."""
    matches = KIND_MARKER_RE.findall(reply_text)
    if not matches:
        return reply_text, None
    stripped = KIND_MARKER_RE.sub("", reply_text).strip() or "(no reply)"
    return stripped, matches[-1].lower()


def _split_sources(reply_text: str) -> tuple[str, list[str]]:
    matches = SOURCES_MARKER_RE.findall(reply_text)
    if not matches:
        return reply_text, []
    stripped = SOURCES_MARKER_RE.sub("", reply_text).strip() or "(no reply)"
    names = [name.strip().strip("`'\"") for match in matches for name in match.split(",")]
    return stripped, [name for name in names if name]


def _normalize_for_repeat(text: str) -> str:
    lines = [line for line in text.splitlines() if not line.startswith("-# 📖")]
    return " ".join(" ".join(lines).lower().split())


def _is_repeat(reply: str, previous_replies: list[str]) -> bool:
    candidate = _normalize_for_repeat(reply)
    if len(candidate) < REPEAT_MIN_CHARS:
        return False
    return any(
        difflib.SequenceMatcher(None, candidate, _normalize_for_repeat(previous)).ratio() > REPEAT_SIMILARITY
        for previous in previous_replies[-3:]
    )


def _append_wiki_sources(
    reply_text: str,
    cited_filenames: list[str],
    *,
    base_url: str,
) -> str:
    if not reply_text or reply_text == "(no reply)":
        return reply_text

    links: list[str] = []
    seen_urls: set[str] = set()
    for filename in cited_filenames:
        mapped = wiki_url_for_citation_filename(filename, base_url=base_url)
        if mapped is None:
            continue
        title, url = mapped
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # <...> suppresses Discord link embeds; -# renders as subtext.
        links.append(f"[{title}](<{url}>)")
        if len(links) >= MAX_WIKI_SOURCE_LINKS:
            break

    if not links:
        return reply_text
    return f"{reply_text}\n-# 📖 {' · '.join(links)}"


def _hydrate_tool_args(
    *,
    name: str,
    args: dict[str, Any],
    user_id: int,
    requester_is_staff: bool = False,
    channel_kind: str | None = None,
) -> dict[str, Any]:
    """Identity comes from Discord, never from the model: other users' data only for staff in DMs,
    so nothing personal is shown to bystanders in tickets or public channels."""
    hydrated = dict(args)
    if name in ai_tools.IDENTITY_TOOLS:
        requested = str(hydrated.get("discord_user_id") or "").strip()
        allowed = requester_is_staff and channel_kind == "dm" and requested.isdigit()
        hydrated["discord_user_id"] = requested if allowed else str(user_id)
    return hydrated


def _select_reasoning_effort(settings: Any, *, high_confidence: bool = False) -> str:
    default_effort = getattr(settings, "openai_support_reasoning_effort", "medium")
    fast_effort = getattr(settings, "openai_support_fast_reasoning_effort", default_effort)
    if high_confidence:
        return fast_effort
    return default_effort


def _support_vector_store_ids(settings: Any) -> list[str]:
    return [
        str(value).strip()
        for value in getattr(settings, "openai_support_vector_store_ids", ())
        if str(value).strip()
    ]


def _file_search_max_results(settings: Any) -> int:
    try:
        return int(getattr(settings, "openai_support_file_search_max_results", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _build_file_search_tool(settings: Any) -> dict[str, Any] | None:
    vector_store_ids = _support_vector_store_ids(settings)
    if not vector_store_ids:
        return None
    tool: dict[str, Any] = {
        "type": "file_search",
        "vector_store_ids": vector_store_ids,
    }
    max_results = _file_search_max_results(settings)
    if max_results > 0:
        tool["max_num_results"] = max_results
    return tool


async def _search_knowledge(query: str, settings: Any) -> list[dict[str, str]]:
    """Retrieval for the tool-free pass (tool use is excluded from the free token pool)."""
    vector_store_ids = _support_vector_store_ids(settings)
    if not vector_store_ids or not query:
        return []
    max_results = _file_search_max_results(settings) or 5
    try:
        pages = await asyncio.gather(
            *(
                client.vector_stores.search(
                    vector_store_id=vector_store_id,
                    query=query,
                    max_num_results=max_results,
                    rewrite_query=True,
                )
                for vector_store_id in vector_store_ids
            )
        )
    except Exception:
        log.exception("Knowledge search failed", extra={"event": "support_knowledge_search_failed"})
        return []
    hits = sorted(
        (hit for page in pages for hit in getattr(page, "data", []) or []),
        key=lambda hit: getattr(hit, "score", 0.0) or 0.0,
        reverse=True,
    )
    return [
        {
            "filename": str(hit.filename),
            "text": "\n".join(
                getattr(part, "text", "") for part in hit.content or [] if getattr(part, "type", None) == "text"
            )[:MAX_KNOWLEDGE_CHUNK_CHARS],
        }
        for hit in hits[:max_results]
    ]


async def _prefetch(bot: Any, user_id: int, text: str) -> list[str]:
    if bot is None:
        return []
    try:
        return await ai_tools.build_prefetch_lines(bot=bot, user_id=user_id, text=text)
    except Exception:
        log.exception("Support prefetch failed", extra={"event": "support_prefetch_failed"})
        return []


def _build_prompt_cache_key(*, model: str, tools: list[dict[str, Any]]) -> str:
    tool_signature = hashlib.sha256(
        json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return f"support:{model}:{tool_signature}"


async def _create_response(*, timeout_seconds: int, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        client.responses.create(**kwargs),
        timeout=timeout_seconds,
    )


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


def _sum_usage(responses: list[Any]) -> dict[str, int | None]:
    totals: dict[str, int | None] = {}
    for response in responses:
        for key, value in _extract_response_usage(response).items():
            if value is not None:
                totals[key] = (totals.get(key) or 0) + value
            else:
                totals.setdefault(key, None)
    return totals


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


def _is_clarify_loop(result: AgentResult, previous_replies: list[str]) -> bool:
    """A second question in a row: paraphrased re-asks slip past the repeat check."""
    # ponytail: "?" anywhere in the last reply counts as a question; a false hit only costs an escalation.
    return result.get("kind") == "clarify" and bool(previous_replies) and "?" in previous_replies[-1]


def _needs_escalation(result: AgentResult, *, repeat: bool, threshold: float, clarify_loop: bool = False) -> bool:
    if repeat or clarify_loop or result.get("reply") == "(no reply)":
        return True
    kind = result.get("kind")
    confidence = result.get("confidence")
    if kind == "handoff":
        return True
    return kind == "answer" and confidence is not None and confidence < threshold


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
    context_lines: list[str] | None = None,
    requester_is_staff: bool = False,
    channel_kind: str | None = None,
) -> AgentResult:
    """Tool-free first pass on the fast model (free pool), escalating when it is unsure or repeats itself."""
    runtime_settings = settings or load_settings()
    last_user = _latest_user_message(messages, target_speaker_id=str(user_id))
    if language_hint:
        language = language_hint
    elif last_user:
        language = detect_language_from_text(last_user["content"])
    else:
        language = "en"

    preferred = model_override or runtime_settings.openai_support_model or runtime_settings.openai_model
    model = ai_budget.pick_model(preferred, runtime_settings)
    if model is None:
        return AgentResult(
            reply="(no reply)",
            language=language,
            tool_results=[],
            suggested_close=False,
            confidence=None,
            kind=None,
            model="",
            escalated=False,
            paused=True,
        )

    query = _knowledge_query(messages, user_id)
    knowledge, prefetch_lines = await asyncio.gather(
        _search_knowledge(query, runtime_settings),
        _prefetch(bot, user_id, query),
    )
    run = functools.partial(
        _run_once,
        base_input=_build_response_input(messages, context_lines=context_lines),
        data_input=_build_data_input(knowledge, prefetch_lines),
        knowledge_filenames={hit["filename"] for hit in knowledge},
        enabled_tools=enabled_tools,
        language=language,
        settings=runtime_settings,
        user_id=user_id,
        channel_id=channel_id,
        ticket_conversation=ticket_conversation,
        bot=bot,
        requester_is_staff=requester_is_staff,
        channel_kind=channel_kind,
    )
    fast_effort = _select_reasoning_effort(runtime_settings, high_confidence=True)
    result, trace = await run(model=model, effort=fast_effort, use_tools=False, workflow="support_question")
    result["escalated"] = False
    result["paused"] = False

    previous_replies = _assistant_replies(messages)
    repeat = _is_repeat(result["reply"], previous_replies)
    clarify_loop = _is_clarify_loop(result, previous_replies)
    threshold = float(getattr(runtime_settings, "ai_support_escalation_confidence", 0.7))
    route = ai_budget.escalation_route(runtime_settings)
    if (
        not _needs_escalation(result, repeat=repeat, threshold=threshold, clarify_loop=clarify_loop)
        or route is None
        or route == (model, fast_effort, False)
    ):
        await _record_traces(trace)
        return result

    escalation_model, effort, use_tools = route
    try:
        second, second_trace = await run(
            model=escalation_model,
            effort=effort,
            use_tools=use_tools,
            workflow="support_escalation",
            notes=[*([REPEAT_NOTE] if repeat else []), *([CLARIFY_LOOP_NOTE] if clarify_loop else [])],
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # A failed second opinion should not lose the first answer.
        log.exception("Support escalation failed", extra={"event": "support_escalation_failed", "channel_id": channel_id})
        await _record_traces(trace)
        return result
    if second["reply"] == "(no reply)" and not second["tool_results"]:
        await _record_traces(trace, drafts=[second_trace])
        return result
    second["escalated"] = True
    second["paused"] = False
    if _is_repeat(second["reply"], previous_replies) or _is_clarify_loop(second, previous_replies):
        second["kind"] = "handoff"
    await _record_traces(second_trace, drafts=[trace])
    return second


async def _record_traces(sent: SupportAITrace, *, drafts: list[SupportAITrace] = ()) -> None:
    """The sent reply keeps its workflow; replaced drafts become `support_draft` so eval/FAQ
    exports (which read support_question/support_escalation) only see what users received.
    Drafts still count toward the token budget seeding."""
    for trace in [*(replace(draft, workflow="support_draft") for draft in drafts), sent]:
        try:
            await record_support_ai_trace(trace)
        except Exception:
            log.exception(
                "Failed to record OpenAI support trace",
                extra={"event": "support_trace_record_failed", "channel_id": trace.channel_id},
            )


async def _run_once(
    *,
    model: str,
    effort: str,
    use_tools: bool,
    workflow: str,
    base_input: list[dict[str, str]],
    data_input: list[dict[str, str]],
    knowledge_filenames: set[str],
    enabled_tools: list[str],
    language: str,
    settings: Any,
    user_id: int,
    channel_id: int,
    ticket_conversation: bool,
    bot: Any,
    requester_is_staff: bool,
    channel_kind: str | None,
    notes: list[str] | None = None,
) -> tuple[AgentResult, SupportAITrace]:
    store = bool(getattr(settings, "openai_support_store_responses", True))
    tools: list[dict[str, Any]] = []
    if use_tools:
        # The tool loop chains rounds with previous_response_id, which needs stored responses.
        if store:
            tools.extend(get_schemas(enabled_tools))
        file_search_tool = _build_file_search_tool(settings)
        if file_search_tool is not None:
            tools.append(file_search_tool)
    file_search_enabled = any(tool.get("type") == "file_search" for tool in tools)
    request_input = [*base_input, *data_input, *({"role": "developer", "content": note} for note in notes or [])]
    request_metadata = _build_openai_metadata(
        workflow=workflow,
        language=language,
        channel_id=channel_id,
        user_id=user_id,
        file_search_enabled=file_search_enabled,
        ticket_conversation=ticket_conversation,
    )
    prompt_cache_key = _build_prompt_cache_key(model=model, tools=tools)
    request_kwargs: dict[str, Any] = {
        "model": model,
        "instructions": _load_system_prompt(language),
        "input": request_input,
        "max_output_tokens": settings.openai_support_max_output_tokens,
        "metadata": request_metadata,
        "prompt_cache_key": prompt_cache_key,
        "safety_identifier": _build_safety_identifier(user_id),
        "store": store,
    }
    if tools:
        request_kwargs["tools"] = tools
        request_kwargs["tool_choice"] = "auto"
    # reasoning/verbosity are gpt-5 only; other models 400 on them.
    if model.startswith("gpt-5"):
        request_kwargs["text"] = {"verbosity": "low"}
        request_kwargs["reasoning"] = {"effort": effort, "summary": "auto"}

    started_at = time.perf_counter()
    response = await _create_response(timeout_seconds=settings.ai_support_timeout_seconds, **request_kwargs)
    responses = [response]
    tool_results: list[ToolCallResult] = []
    previous_response_id: str | None = None
    for round_index in range(MAX_TOOL_ROUNDS):
        calls = _extract_function_calls(response)
        if not calls:
            break
        outputs: list[dict[str, Any]] = []
        for call in calls:
            try:
                args = json.loads(call["arguments"]) if isinstance(call["arguments"], str) else call["arguments"]
            except json.JSONDecodeError:
                args = {}
            args = _hydrate_tool_args(
                name=call["name"],
                args=args if isinstance(args, dict) else {},
                user_id=user_id,
                requester_is_staff=requester_is_staff,
                channel_kind=channel_kind,
            )
            try:
                output = await tools_registry.get_func(call["name"], bot_context=bot)(**args)
            except Exception:
                log.exception("Support tool failed", extra={"event": "support_tool_failed", "tool": call["name"]})
                output = {"error": "tool_failed"}
            tool_results.append(ToolCallResult(name=call["name"], arguments=args, output=output))
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(output, ensure_ascii=False, default=str),
                }
            )
        if any(isinstance(r["output"], dict) and r["output"].get("suppress_ai_reply") is True for r in tool_results):
            break
        previous_response_id = getattr(response, "id", None)
        followup_kwargs = {**request_kwargs, "input": outputs, "previous_response_id": previous_response_id}
        if round_index == MAX_TOOL_ROUNDS - 1:
            followup_kwargs["tool_choice"] = "none"
        response = await _create_response(timeout_seconds=settings.ai_support_timeout_seconds, **followup_kwargs)
        responses.append(response)

    for each in responses:
        ai_budget.record_response(each, billed=bool(tools))

    suppressed = any(
        isinstance(r["output"], dict) and r["output"].get("suppress_ai_reply") is True for r in tool_results
    )
    raw_text = "(no reply)" if suppressed else (_extract_output_text(response) or "(no reply)")
    reply_text, confidence = _split_confidence(raw_text)
    reply_text, kind = _split_kind(reply_text)
    reply_text, sources = _split_sources(reply_text)
    # Before the wiki links are appended: those are ours, everything above them is model output.
    reply_text, guard_flags = ai_guard.screen_reply(reply_text, settings)
    if guard_flags - {"unknown_link"}:
        log.warning(
            "AI reply tripped guardrails (%s)%s",
            ", ".join(sorted(guard_flags)),
            "; blocked" if guard_flags & ai_guard.BLOCKING_FLAGS else "; defused",
            extra={"event": "ai_reply_guarded", "channel_id": channel_id, "user_id": user_id, "workflow": workflow},
        )
    cited = [name for each in responses for name in _extract_file_citations(each)]
    cited += [name for name in sources if name in knowledge_filenames]
    reply_text = _append_wiki_sources(
        reply_text,
        list(dict.fromkeys(cited)),
        base_url=getattr(settings, "wiki_base_url", DEFAULT_WIKI_BASE_URL),
    )
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
    usage = _sum_usage(responses)
    trace = SupportAITrace(
        workflow=workflow,
        response_id=response_id,
        openai_conversation_id=None,
        previous_response_id=previous_response_id,
        model=model,
        language=language,
        channel_id=channel_id,
        user_id=user_id,
        prompt_cache_key=prompt_cache_key,
        file_search_enabled=file_search_enabled,
        vector_store_ids=_support_vector_store_ids(settings) if file_search_enabled else [],
        tool_names=_tool_names_for_trace(tools, tool_results),
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        cached_tokens=usage.get("cached_tokens"),
        reasoning_tokens=usage.get("reasoning_tokens"),
        reply_text=reply_text,
        confidence=confidence,
        input_json=request_input,
        request_metadata={**request_metadata, "reply_kind": kind or "", "reasoning_effort": effort},
    )

    result = AgentResult(
        reply=reply_text,
        language=language,
        tool_results=tool_results,
        suggested_close=suggested_close,
        confidence=confidence,
        kind=kind,
        model=model,
        guard_flags=guard_flags,
    )
    if response_id:
        result["response_id"] = response_id
    return result, trace
