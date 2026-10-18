"""Helpers for single-shot Claude Messages calls (summaries, translation, JSON extraction, vision)."""

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def json_output_config(schema: dict[str, Any], effort: str = "low") -> dict[str, Any]:
    return {"effort": effort, "format": {"type": "json_schema", "schema": schema}}


def response_text(response: Any, label: str = "ai") -> str:
    """Concatenated text blocks, or "" when the model refused (a truncated reply is logged and kept)."""
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        log.warning("%s: model refused", label)
        return ""
    if stop_reason == "max_tokens":
        log.warning("%s: hit max_tokens", label)
    return "".join(
        getattr(block, "text", "") or ""
        for block in getattr(response, "content", None) or []
        if getattr(block, "type", None) == "text"
    )


def response_json(response: Any, label: str = "ai") -> Any:
    """Parsed structured output. Raises ValueError on refusal/empty, json errors on bad JSON."""
    text = response_text(response, label).strip()
    if not text:
        raise ValueError(f"{label}: empty or refused AI response")
    return json.loads(text)
