import logging

from dotenv import load_dotenv
from openai import AsyncOpenAI

from bulmaai.config import load_settings

log = logging.getLogger(__name__)


class LLMClient:
    """Service class for interacting with the OpenAI Chat API."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_output_tokens: int = 1500,
    ) -> str:

        input_items = []
        for msg in messages:
            input_items.append(
                {
                    "type": "message",
                    "role": msg[
                        "role"
                    ],  # "user" | "assistant" | "system" | "developer"
                    "content": [
                        {
                            "type": "input_text",
                            "text": msg["content"],
                        }
                    ],
                }
            )

        try:
            response = await self._client.responses.create(
                model=self._model,
                input=input_items,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:
            log.exception("OpenAI chat completion failed: %s", exc)
            raise RuntimeError("LLM request failed") from exc

        # This may be wrong (if check), check later
        if not response.output:
            log.error("OpenAI response contained no output: %r", response)
            raise RuntimeError("LLM response contained no output")

        message_items = [
            item for item in response.output if getattr(item, "type", None) == "message"
        ]
        if not message_items:
            log.error("OpenAI response contained no message output: %r", response)
            raise RuntimeError("LLM response contained no message output")

        text_parts: list[str] = []
        for msg in message_items:
            text_blocks = [
                block for block in getattr(msg, "content", [])
                if getattr(block, "type", None) == "output_text"
            ]
            for block in text_blocks:
                text = getattr(block, "text", None)
                if text is not None:
                    text_parts.append(text)

        content = "".join(text_parts)

        usage = getattr(response, "usage", None)
        input_tokens = (
            getattr(usage, "input_tokens", None) if usage is not None else None
        )
        output_tokens = (
            getattr(usage, "output_tokens", None) if usage is not None else None
        )

        log.info(
            "LLM call success | model=%s input_tokens=%s output_tokens=%s",
            getattr(response, "model", self._model),
            input_tokens,
            output_tokens,
        )
        return content


load_dotenv()
settings = load_settings()

llm_client = LLMClient(
    api_key=settings.openai_key,
    model=settings.openai_model,
)
