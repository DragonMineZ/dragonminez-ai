import json
from dataclasses import dataclass
from hmac import compare_digest
from typing import Any, Callable

from bulmaai.services.dev_jar_downloads import (
    DevJarUploadPayload,
    parse_dev_jar_upload_payload,
)


DEV_JAR_WEBHOOK_SECRET_HEADER = "X-DMZ-Dev-Jar-Secret"


@dataclass(frozen=True)
class DevJarWebhookResponse:
    status: int
    body: str


FORBIDDEN_RESPONSE = DevJarWebhookResponse(status=403, body="Forbidden")


def _get_header(headers: Any, name: str) -> str | None:
    if hasattr(headers, "get"):
        return headers.get(name)
    return None


def _has_valid_secret(headers: Any, secret: str | None) -> bool:
    if not secret:
        return False
    provided_secret = _get_header(headers, DEV_JAR_WEBHOOK_SECRET_HEADER)
    return bool(provided_secret) and compare_digest(provided_secret, secret)


def handle_dev_jar_webhook_post(
    *,
    path: str,
    body: bytes,
    headers: Any,
    expected_path: str,
    secret: str | None,
    submit_payload: Callable[[DevJarUploadPayload], None],
) -> DevJarWebhookResponse:
    if not _has_valid_secret(headers, secret):
        return FORBIDDEN_RESPONSE

    if path != expected_path:
        return DevJarWebhookResponse(status=404, body="Not found")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return DevJarWebhookResponse(status=400, body="Invalid JSON body")

    if not isinstance(payload, dict):
        return DevJarWebhookResponse(status=400, body="JSON body must be an object")

    try:
        parsed = parse_dev_jar_upload_payload(payload)
    except ValueError as error:
        return DevJarWebhookResponse(status=400, body=str(error))

    submit_payload(parsed)
    return DevJarWebhookResponse(status=202, body="Dev jar upload queued")
