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

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    InternalServerError,
    RateLimitError,
)

from bulmaai.config import Settings, load_settings
from bulmaai.services import ai_budget, ai_guard, ai_tools, knowledge, support_info
from bulmaai.services.support_traces import SupportAITrace, record_support_ai_trace
from bulmaai.utils.language import detect_language_from_text
from bulmaai.utils import tools_registry

client = AsyncAnthropic(api_key=load_settings().anthropic_api_key)
log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3
DEFAULT_KNOWLEDGE_MAX_RESULTS = 5
# Server-side refusal fallback ("default" picks Anthropic's recommended model per refusal category).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTINUE_TURN = "(continue the support conversation)"
START_TURN = "(start of the support conversation)"
SEARCH_KNOWLEDGE_TOOL = "search_knowledge"
SEARCH_KNOWLEDGE_SCHEMA: dict[str, Any] = {
    "name": SEARCH_KNOWLEDGE_TOOL,
    "description": (
        "Search DragonMineZ wiki pages, solved support tickets and the FAQ. Write the query in English "
        "(a few keywords: feature, item, error or symptom)."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search keywords, in English."}},
        "required": ["query"],
        "additionalProperties": False,
    },
}
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
    ask: dict[str, Any] | None  # ask_requester / offer_action controls for the sent reply (ui/ai_asks spec)


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


def _build_conversation(
    messages: list[ConversationMessage],
    *,
    context_lines: list[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """(context text for the system prompt, conversation turns). The turns start and end on a user turn,
    as the Messages API needs (no assistant prefill)."""
    merged = _merge_assistant_turns(messages)
    context = [
        *(context_lines or []),
        f"today (UTC): {datetime.now(timezone.utc).date().isoformat()}",
        f"your earlier replies in this transcript: {sum(1 for m in merged if m.get('role') == 'assistant')}",
    ]
    turns: list[dict[str, str]] = []
    for message in merged:
        content = _message_to_input_content(message)
        if content:
            role = "assistant" if message.get("role") == "assistant" else "user"
            turns.append({"role": role, "content": content})
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": START_TURN})
    if turns[-1]["role"] != "user":
        turns.append({"role": "user", "content": CONTINUE_TURN})
    return "Context:\n" + "\n".join(f"- {line}" for line in context), turns


SUPPORT_INFO_HEADER = (
    "Support info from DragonMineZ staff (current and authoritative; when it matches the question, answer from it "
    "and prefer it over knowledge search; temporary entries describe current issues):\n\n"
)


async def _support_info_text() -> str:
    """Staff support info for the system prompt; a DB failure only drops the section."""
    try:
        return support_info.render_for_ai(await support_info.cached_active())
    except Exception:
        log.exception("Support info lookup failed; answering without it")
        return ""


def _build_data_input(
    knowledge: list[dict[str, str]], prefetch_lines: list[str], support_info_text: str = ""
) -> str:
    parts: list[str] = []
    if support_info_text:
        parts.append(SUPPORT_INFO_HEADER + support_info_text)
    if prefetch_lines:
        parts.append(
            "Looked-up data (live and authoritative; requester_* entries describe the requester only):\n"
            + "\n".join(prefetch_lines)
        )
    if knowledge:
        parts.append(
            "Knowledge search results for this conversation are attached to the latest user turn as search results "
            "(from the bot's search, not written by the requester)."
        )
    return "\n\n".join(parts)


def _search_result_blocks(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Knowledge hits as citable `search_result` blocks; every block in a request must share the citations setting."""
    return [
        {
            "type": "search_result",
            "source": hit.get("url") or hit["filename"],
            "title": hit.get("title") or hit["filename"],
            "content": [{"type": "text", "text": hit["text"]}],
            "citations": {"enabled": True},
        }
        for hit in hits
        if hit.get("text")
    ]


def _with_knowledge(conversation: list[dict[str, Any]], hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefetched hits go in front of the latest user turn: search results are not allowed in `system`."""
    blocks = _search_result_blocks(hits)
    if not blocks:
        return conversation
    *head, last = conversation
    return [*head, {"role": "user", "content": [*blocks, {"type": "text", "text": last["content"]}]}]


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


def _extract_tool_uses(response: Any) -> list[Any]:
    """The `tool_use` blocks of a response that stopped to call tools."""
    if getattr(response, "stop_reason", None) != "tool_use":
        return []
    return [block for block in getattr(response, "content", []) or [] if getattr(block, "type", None) == "tool_use"]


def _extract_text(response: Any) -> str:
    return "".join(
        getattr(block, "text", "") or ""
        for block in getattr(response, "content", []) or []
        if getattr(block, "type", None) == "text"
    ).strip()


MAX_WIKI_SOURCE_LINKS = 3
CONFIDENCE_MARKER_RE = re.compile(r"\[\s*confidence\s*[:=]\s*([0-9]*\.?[0-9]+)\s*\]", re.IGNORECASE)
KIND_MARKER_RE = re.compile(r"\[\s*kind\s*[:=]\s*(answer|clarify|handoff|offtopic)\s*\]", re.IGNORECASE)
CLOSE_MARKER_RE = re.compile(r"\[\s*close\s*[:=]\s*yes\s*\]", re.IGNORECASE)


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


def _split_close(reply_text: str) -> tuple[str, bool]:
    """Strip every `[close: yes]` marker: the model thinks the requester is done with the ticket."""
    if not CLOSE_MARKER_RE.search(reply_text):
        return reply_text, False
    return CLOSE_MARKER_RE.sub("", reply_text).strip() or "(no reply)", True


def _last_ask(tool_results: list[ToolCallResult]) -> dict[str, Any] | None:
    """The controls of the last valid ask_requester or offer_action call (its output is already validated, see
    ui/ai_asks): one set per reply."""
    for result in reversed(tool_results):
        output = result["output"]
        if result["name"] in ai_tools.CONTROL_TOOLS and isinstance(output, dict) and output.get("ok") is True:
            return output.get("shown")
    return None


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


PART_SUFFIX_RE = re.compile(r"\s*\(\d+/\d+\)$")


def _cited_sources(response: Any) -> list[tuple[str, str]]:
    """(title, source) of every API citation in the reply, in reply order."""
    return [
        (str(getattr(citation, "title", None) or ""), str(getattr(citation, "source", None) or ""))
        for block in getattr(response, "content", []) or []
        if getattr(block, "type", None) == "text"
        for citation in getattr(block, "citations", None) or []
        if getattr(citation, "type", None) == "search_result_location"
    ]


def _append_source_links(reply_text: str, cited: list[tuple[str, str]]) -> str:
    """One link per cited page (wiki sections share a page URL up to the #anchor); non-URL sources get none."""
    if not reply_text or reply_text == "(no reply)":
        return reply_text

    links: list[str] = []
    seen_pages: set[str] = set()
    for title, url in cited:
        page = url.split("#", 1)[0]
        if not url.startswith(("https://", "http://")) or page in seen_pages:
            continue
        seen_pages.add(page)
        title = PART_SUFFIX_RE.sub("", title) or page
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
    so nothing personal is shown to bystanders in tickets or public channels. offer_action learns where it is the
    same way (which actions can run there)."""
    hydrated = dict(args)
    if name == ai_tools.ACTION_TOOL:
        hydrated["channel_kind"] = channel_kind
    if name in ai_tools.IDENTITY_TOOLS:
        requested = str(hydrated.get("discord_user_id") or "").strip()
        allowed = requester_is_staff and channel_kind == "dm" and requested.isdigit()
        hydrated["discord_user_id"] = requested if allowed else str(user_id)
    return hydrated


def _select_reasoning_effort(settings: Any, *, high_confidence: bool = False) -> str:
    default_effort = getattr(settings, "ai_support_effort", "medium")
    fast_effort = getattr(settings, "ai_support_fast_effort", default_effort)
    if high_confidence:
        return fast_effort
    return default_effort


def _knowledge_max_results(settings: Any) -> int:
    try:
        return int(getattr(settings, "ai_knowledge_max_results", 0) or 0) or DEFAULT_KNOWLEDGE_MAX_RESULTS
    except (TypeError, ValueError):
        return DEFAULT_KNOWLEDGE_MAX_RESULTS


async def _search_knowledge(query: str, settings: Any) -> list[dict[str, Any]]:
    """Prefetch retrieval before the first call; a failure only drops the section."""
    if not query:
        return []
    try:
        return await knowledge.search(query, _knowledge_max_results(settings))
    except Exception:
        log.exception("Knowledge search failed", extra={"event": "support_knowledge_search_failed"})
        return []


async def _run_knowledge_tool(args: dict[str, Any], settings: Any) -> list[dict[str, Any]]:
    """The `search_knowledge` client tool. Raises on a DB failure (reported to the model as is_error)."""
    query = str(args.get("query") or "").strip()
    return await knowledge.search(query, _knowledge_max_results(settings)) if query else []


async def _prefetch(bot: Any, user_id: int, text: str, account_key: Any = None) -> list[str]:
    if bot is None:
        return []
    try:
        return await ai_tools.build_prefetch_lines(bot=bot, user_id=user_id, text=text, account_key=account_key)
    except Exception:
        log.exception("Support prefetch failed", extra={"event": "support_prefetch_failed"})
        return []


def _uses_server_fallback(model: str) -> bool:
    """Haiku has no server-side refusal fallback; its refusals become "(no reply)"."""
    return "haiku" not in (model or "").lower()


async def _create_message(*, timeout_seconds: int, **kwargs: Any) -> Any:
    if _uses_server_fallback(kwargs.get("model", "")):
        call = client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
    else:
        call = client.messages.create(**kwargs)
    return await asyncio.wait_for(call, timeout=timeout_seconds)


def is_transient_ai_error(error: BaseException) -> bool:
    if isinstance(error, (asyncio.TimeoutError, APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
        return True
    if isinstance(error, APIStatusError):
        status_code = getattr(error, "status_code", None)
        return status_code in {408, 409, 425, 429} or bool(
            isinstance(status_code, int) and status_code >= 500
        )
    return False


def _build_trace_metadata(
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
    """Claude usage: the total includes both cache counters; there is no reasoning-token count."""
    usage = getattr(response, "usage", None)
    return {
        "input_tokens": _usage_int(usage, "input_tokens"),
        "output_tokens": _usage_int(usage, "output_tokens"),
        "total_tokens": ai_budget.usage_total(usage),
        "cached_tokens": _usage_int(usage, "cache_read_input_tokens"),
        "reasoning_tokens": None,
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


def _reply_suppressed(tool_results: list[ToolCallResult]) -> bool:
    """A tool already answered the requester (e.g. it posted its own card)."""
    return any(isinstance(r["output"], dict) and r["output"].get("suppress_ai_reply") is True for r in tool_results)


def _needs_escalation(result: AgentResult, *, repeat: bool, threshold: float, clarify_loop: bool = False) -> bool:
    if _reply_suppressed(result.get("tool_results") or []):
        return False
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
    workflow_prefix: str | None = None,
) -> AgentResult:
    """Fast first pass on the support model, escalating when it is unsure, repeats itself or loops on questions.
    workflow_prefix renames the traces (prefix, prefix_escalation, prefix_draft) so non-support callers stay out
    of the support_* workflows the FAQ/eval exports read."""
    first_workflow, escalation_workflow, draft_workflow = (
        (workflow_prefix, f"{workflow_prefix}_escalation", f"{workflow_prefix}_draft")
        if workflow_prefix
        else ("support_question", "support_escalation", "support_draft")
    )
    runtime_settings = settings or load_settings()
    last_user = _latest_user_message(messages, target_speaker_id=str(user_id))
    if language_hint:
        language = language_hint
    elif last_user:
        language = detect_language_from_text(last_user["content"])
    else:
        language = "en"

    preferred = model_override or runtime_settings.ai_support_model or runtime_settings.ai_model
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
    knowledge, prefetch_lines, support_info_text = await asyncio.gather(
        _search_knowledge(query, runtime_settings),
        _prefetch(bot, user_id, query, (channel_id, user_id) if ticket_conversation else None),
        _support_info_text(),
    )
    context_text, conversation = _build_conversation(messages, context_lines=context_lines)
    run = functools.partial(
        _run_once,
        context_text=context_text,
        conversation=conversation,
        data_text=_build_data_input(knowledge, prefetch_lines, support_info_text),
        staff_links=ai_guard.link_targets(support_info_text),
        knowledge_hits=knowledge,
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
    result, trace = await run(model=model, effort=fast_effort, use_tools=True, workflow=first_workflow)
    result["escalated"] = False
    result["paused"] = False

    previous_replies = _assistant_replies(messages)
    repeat = _is_repeat(result["reply"], previous_replies)
    clarify_loop = _is_clarify_loop(result, previous_replies)
    threshold = float(getattr(runtime_settings, "ai_support_escalation_confidence", 0.7))
    notes = [*([REPEAT_NOTE] if repeat else []), *([CLARIFY_LOOP_NOTE] if clarify_loop else [])]
    drafts: list[SupportAITrace] = []
    # Haiku at high effort settles most escalations; Sonnet only sees what it still can't.
    for escalation_model, effort, use_tools in ai_budget.escalation_routes(runtime_settings):
        if not _needs_escalation(result, repeat=repeat, threshold=threshold, clarify_loop=clarify_loop):
            break
        if (escalation_model, effort) == (model, fast_effort):
            continue
        try:
            second, second_trace = await run(
                model=escalation_model, effort=effort, use_tools=use_tools, workflow=escalation_workflow, notes=notes
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed second opinion should not lose the answer so far.
            log.exception("Support escalation failed", extra={"event": "support_escalation_failed", "channel_id": channel_id})
            break
        if second["reply"] == "(no reply)" and not any(
            call["name"] not in (SEARCH_KNOWLEDGE_TOOL, *ai_tools.CONTROL_TOOLS) for call in second["tool_results"]
        ):
            drafts.append(second_trace)
            continue
        second["escalated"] = True
        second["paused"] = False
        if second.get("kind") is None:
            # A second pass sometimes drops the marker; a handoff must not silently become a plain reply.
            second["kind"] = result.get("kind")
        if _is_repeat(second["reply"], previous_replies) or _is_clarify_loop(second, previous_replies):
            second["kind"] = "handoff"
        if second["kind"] == "handoff":
            second["ask"] = None
        drafts.append(trace)
        result, trace = second, second_trace
        repeat = clarify_loop = False  # folded into the handoff above
    await _record_traces(trace, drafts=drafts, draft_workflow=draft_workflow)
    return result


async def _record_traces(
    sent: SupportAITrace, *, drafts: list[SupportAITrace] = (), draft_workflow: str = "support_draft"
) -> None:
    """The sent reply keeps its workflow; replaced drafts become `support_draft` so eval/FAQ
    exports (which read support_question/support_escalation) only see what users received.
    Drafts still count toward the token budget seeding."""
    for trace in [*(replace(draft, workflow=draft_workflow) for draft in drafts), sent]:
        try:
            await record_support_ai_trace(trace)
        except Exception:
            log.exception(
                "Failed to record support AI trace",
                extra={"event": "support_trace_record_failed", "channel_id": trace.channel_id},
            )


async def _run_once(
    *,
    model: str,
    effort: str,
    use_tools: bool,
    workflow: str,
    context_text: str,
    conversation: list[dict[str, str]],
    data_text: str,
    knowledge_hits: list[dict[str, Any]],
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
    staff_links: frozenset[str] = frozenset(),
) -> tuple[AgentResult, SupportAITrace]:
    tools: list[dict[str, Any]] = []
    if use_tools:
        tools.extend(get_schemas(enabled_tools))
        tools.append(SEARCH_KNOWLEDGE_SCHEMA)
    knowledge_tool_enabled = any(tool.get("name") == SEARCH_KNOWLEDGE_TOOL for tool in tools)
    # Volatile text (date, data, notes) goes after the cached prompt so the prefix stays cacheable.
    dynamic_text = "\n\n".join(part for part in (context_text, data_text, *(notes or [])) if part)
    system = [
        {"type": "text", "text": _load_system_prompt(language), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic_text},
    ]
    request_metadata = _build_trace_metadata(
        workflow=workflow,
        language=language,
        channel_id=channel_id,
        user_id=user_id,
        file_search_enabled=knowledge_tool_enabled,
        ticket_conversation=ticket_conversation,
    )
    request_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": settings.ai_support_max_output_tokens,
        "system": system,
        "messages": _with_knowledge(conversation, knowledge_hits),
        "output_config": {"effort": effort},
        "metadata": {"user_id": _build_safety_identifier(user_id)},
        # Caches the conversation too, so each tool round re-reads the previous round's prefix at 0.1x.
        "cache_control": {"type": "ephemeral"},
    }
    if tools:
        request_kwargs["tools"] = tools
        request_kwargs["tool_choice"] = {"type": "auto"}

    started_at = time.perf_counter()
    response = await _create_message(timeout_seconds=settings.ai_support_timeout_seconds, **request_kwargs)
    responses = [response]
    tool_results: list[ToolCallResult] = []
    turns: list[Any] = list(request_kwargs["messages"])
    for round_index in range(MAX_TOOL_ROUNDS):
        calls = _extract_tool_uses(response)
        if not calls:
            break
        result_blocks: list[dict[str, Any]] = []
        for call in calls:
            args = call.input if isinstance(call.input, dict) else {}
            is_error = False
            try:
                if call.name == SEARCH_KNOWLEDGE_TOOL:
                    args = {"query": str(args.get("query") or "")}
                    hits = await _run_knowledge_tool(args, settings)
                    output = {"results": [{"filename": hit["filename"], "title": hit["title"]} for hit in hits]}
                else:
                    args = _hydrate_tool_args(
                        name=call.name,
                        args=args,
                        user_id=user_id,
                        requester_is_staff=requester_is_staff,
                        channel_kind=channel_kind,
                    )
                    output = await tools_registry.get_func(call.name, bot_context=bot)(**args)
            except Exception:
                log.exception("Support tool failed", extra={"event": "support_tool_failed", "tool": call.name})
                output = {"error": "tool_failed"}
                is_error = True
            tool_results.append(ToolCallResult(name=call.name, arguments=args, output=output))
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps(output, ensure_ascii=False, default=str),
            }
            if call.name == SEARCH_KNOWLEDGE_TOOL and not is_error:
                # A tool_result holding search results may hold nothing else.
                block["content"] = _search_result_blocks(hits) or [{"type": "text", "text": "No results found."}]
            if is_error:
                block["is_error"] = True
            result_blocks.append(block)
        if _reply_suppressed(tool_results):
            break
        # The full content goes back unchanged: thinking (and fallback) blocks must be echoed as they came.
        turns = [*turns, {"role": "assistant", "content": response.content}, {"role": "user", "content": result_blocks}]
        followup_kwargs = {**request_kwargs, "messages": turns}
        if round_index == MAX_TOOL_ROUNDS - 1:
            followup_kwargs["tool_choice"] = {"type": "none"}
        response = await _create_message(timeout_seconds=settings.ai_support_timeout_seconds, **followup_kwargs)
        responses.append(response)

    for each in responses:
        ai_budget.record_response(each)

    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        log.info(
            "Support model refused (%s)",
            getattr(getattr(response, "stop_details", None), "category", None) or "no category",
            extra={"event": "support_ai_refusal", "channel_id": channel_id, "model": model, "workflow": workflow},
        )
    elif stop_reason == "max_tokens":
        log.warning(
            "Support reply hit max_tokens (%s)",
            settings.ai_support_max_output_tokens,
            extra={"event": "support_ai_max_tokens", "channel_id": channel_id, "model": model, "workflow": workflow},
        )

    if _reply_suppressed(tool_results) or stop_reason == "refusal":
        raw_text = "(no reply)"
    else:
        raw_text = _extract_text(response) or "(no reply)"
    reply_text, confidence = _split_confidence(raw_text)
    reply_text, kind = _split_kind(reply_text)
    reply_text, suggested_close = _split_close(reply_text)
    # Before the wiki links are appended: those are ours, everything above them is model output.
    reply_text, guard_flags = ai_guard.screen_reply(reply_text, settings, staff_links)
    # Controls only go with a reply that is actually sent (a blocked one is "(no reply)"), never with a handoff.
    ask = None if kind == "handoff" or reply_text == "(no reply)" else _last_ask(tool_results)
    if guard_flags - {"unknown_link"}:
        log.warning(
            "AI reply tripped guardrails (%s)%s",
            ", ".join(sorted(guard_flags)),
            "; blocked" if guard_flags & ai_guard.BLOCKING_FLAGS else "; defused",
            extra={"event": "ai_reply_guarded", "channel_id": channel_id, "user_id": user_id, "workflow": workflow},
        )
    reply_text = _append_source_links(reply_text, _cited_sources(response))

    response_id = getattr(response, "id", None)
    usage = _sum_usage(responses)
    trace = SupportAITrace(
        workflow=workflow,
        response_id=response_id,
        openai_conversation_id=None,
        previous_response_id=None,
        model=model,
        language=language,
        channel_id=channel_id,
        user_id=user_id,
        prompt_cache_key=None,
        file_search_enabled=knowledge_tool_enabled,
        vector_store_ids=[],
        tool_names=_tool_names_for_trace(tools, tool_results),
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        cached_tokens=usage.get("cached_tokens"),
        reasoning_tokens=None,
        reply_text=reply_text,
        confidence=confidence,
        input_json=[{"role": "system", "content": dynamic_text}, *request_kwargs["messages"]],
        request_metadata={
            **request_metadata,
            "reply_kind": kind or "",
            "reasoning_effort": effort,
            "stop_reason": str(stop_reason or ""),
        },
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
        ask=ask,
    )
    if response_id:
        result["response_id"] = response_id
    return result, trace
