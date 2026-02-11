import logging
import json

from typing import Any, Optional, TypedDict

from dotenv import load_dotenv
from openai import OpenAI

from bulmaai.config import load_settings
from bulmaai.utils import tools_registry

load_dotenv()
settings = load_settings()

client = OpenAI(api_key=settings.openai_key)
log = logging.getLogger(__name__)


class ToolCallResult(TypedDict):
    name: str
    arguments: dict[str, Any]
    output: Any


class AgentResult(TypedDict):
    reply: str
    language: str           # 'en', 'es', 'pt' (best guess)
    tool_results: list[ToolCallResult]
    suggested_close: bool

def get_schemas(enabled_tools: list[str]) -> list[dict]:
    """
    Return tool schemas for the given tool names, in Responses API format.
    """
    return tools_registry.get_schemas(enabled_tools)

def _detect_language_from_text(text: str) -> str:
    # TODO: replace with real detection; for now default to English
    return "en"


def _collapse_history_to_text(messages, user_id: int, channel_id: int) -> str:
    parts = [
        f"Conversation meta: discord_user_id={user_id}, ticket_channel_id={channel_id}"
    ]
    for m in messages:
        role = m.get("role", "user")
        prefix = "User:" if role == "user" else "Assistant:"
        parts.append(f"{prefix} {m.get('content','')}")
    return "\n".join(parts)



async def run_support_agent(
    *,
    messages: list[dict[str, str]],
    enabled_tools: list[str],
    language_hint: Optional[str] = None,
    user_id: int,
    channel_id: int,
    bot: Any = None,  # Discord bot instance for tool context
) -> AgentResult:
    """
    High-level entrypoint for the support agent.

    messages: [{"role": "user"/"assistant", "content": "..."}]
    enabled_tools: tool names registered.
    bot: Discord bot instance to pass to tools that need it.
    """
    model = settings.openai_model

    # Language detection
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if language_hint:
        lang = language_hint
    elif last_user:
        lang = _detect_language_from_text(last_user["content"])
    else:
        lang = "en"

    system_prompt = _load_system_prompt(lang)

    tools = get_schemas(enabled_tools)

    # Flatten conversation into one input string
    transcript = _collapse_history_to_text(messages, user_id=user_id, channel_id=channel_id)

    # First call: allow tools
    response = client.responses.create(
        model=model,
        instructions=system_prompt,
        input=transcript,
        tools=tools,
        tool_choice="auto",
    )

    log.debug(f"First Call OPENAI RESPONSE: {response}")

    # Handle function calls (if any), then get final answer
    return await _handle_tools_and_final_reply(
        response=response,
        base_transcript=transcript,
        system_prompt=system_prompt,
        model=model,
        enabled_tools=enabled_tools,
        lang=lang,
        bot=bot,
    )


async def _handle_tools_and_final_reply(
    *,
    response: Any,
    base_transcript: str,
    system_prompt: str,
    model: str,
    enabled_tools: list[str],
    lang: str,
    bot: Any = None,
) -> AgentResult:
    tool_results: list[ToolCallResult] = []
    transcript = base_transcript

    def extract_function_calls(resp: Any) -> list[dict]:
        """
        Extract function tool calls from Responses API output.
        We expect items with type == 'function_call'. [web:240][web:254]
        """
        calls: list[dict] = []
        for item in getattr(resp, "output", []) or []:
            if item.type == "function_call":
                # item.name, item.arguments (JSON string)
                calls.append(
                    {
                        "name": item.name,
                        "arguments": getattr(item, "arguments", "{}"),
                        "call_id": getattr(item, "id", None),
                    }
                )
        return calls

    # 1) Execute tool calls if present
    function_calls = extract_function_calls(response)

    if function_calls:
        for call in function_calls:
            name = call["name"]
            raw_args = call["arguments"]
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}

            # Get function with bot context injected
            func = tools_registry.get_func(name, bot_context=bot)
            output = await func(**args)

            tool_results.append(
                ToolCallResult(
                    name=name,
                    arguments=args,
                    output=output,
                )
            )

            # Append a synthetic line to the transcript so the model sees tool result
            transcript += f"\nTool {name} output:\n{json.dumps(output, ensure_ascii=False)}\n"

            log.info("RAW TRANSCRIPT OPENAI RESPONSE: %r", transcript)

        # 2) Second call: no tools, just generate final reply using updated transcript
        response = client.responses.create(
            model=model,
            instructions=system_prompt,
            input=transcript,
        )

    log.info("RAW OPENAI RESPONSE: %r", response)

    # 3) Extract final reply text
    reply_text = ""

    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "message":
            for part in getattr(item, "content", []) or []:
                if getattr(part, "type", None) == "output_text":
                    reply_text += getattr(part, "text", "")

    reply_text = reply_text.strip() or "(no reply)"

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


def _load_system_prompt(lang: str) -> str:
    """
    Load language-specific system prompt from configs/prompts.
    Fallback to EN.
    """
    import importlib.resources as pkg_resources

    lang_code = lang if lang in {"en", "es", "pt"} else "en"
    filename = f"support_system_{lang_code}.txt"

    try:
        with pkg_resources.files("bulmaai.configs.prompts").joinpath(filename).open(
            "r", encoding="utf-8"
        ) as f:
            return f.read()
    except FileNotFoundError:
        return (
            "You are DragonMineZ's support assistant. You only answer based on the "
            "provided documentation and tool outputs. If unsure, escalate to human "
            "staff. Respond in the user's language (English, Spanish, or Portuguese)."
        )
