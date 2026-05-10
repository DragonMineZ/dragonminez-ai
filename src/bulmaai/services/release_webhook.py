import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from bulmaai.services.release_approval import (
    ReleaseCandidateError,
    parse_release_candidate_payload,
)


log = logging.getLogger(__name__)

WEBHOOK_SECRET_HEADER = "X-DMZ-Release-Bot-Secret"


@dataclass(frozen=True)
class ReleaseWebhookResponse:
    status: int
    body: str


@dataclass(frozen=True)
class ExtraWebhookRoute:
    path: str
    secret: str
    secret_header: str
    parse_payload: Callable[[dict[str, Any]], Any]
    submit_payload: Callable[[Any], None]
    accepted_body: str


FORBIDDEN_RESPONSE = ReleaseWebhookResponse(status=403, body="Forbidden")

_extra_routes: dict[str, ExtraWebhookRoute] = {}


def register_extra_webhook_route(
    *,
    path: str,
    secret: str | None,
    secret_header: str,
    parse_payload: Callable[[dict[str, Any]], Any],
    submit_payload: Callable[[Any], None],
    accepted_body: str,
) -> None:
    if not secret:
        return
    _extra_routes[path] = ExtraWebhookRoute(
        path=path,
        secret=secret,
        secret_header=secret_header,
        parse_payload=parse_payload,
        submit_payload=submit_payload,
        accepted_body=accepted_body,
    )


def unregister_extra_webhook_route(path: str) -> None:
    _extra_routes.pop(path, None)


def clear_extra_webhook_routes() -> None:
    _extra_routes.clear()


def _get_header(headers: Any, name: str) -> str | None:
    if hasattr(headers, "get"):
        return headers.get(name)
    return None


def _has_valid_secret(headers: Any, secret: str | None) -> bool:
    if not secret:
        return False
    provided_secret = _get_header(headers, WEBHOOK_SECRET_HEADER)
    return bool(provided_secret) and compare_digest(provided_secret, secret)


def _has_valid_named_secret(headers: Any, *, secret: str | None, header_name: str) -> bool:
    if not secret:
        return False
    provided_secret = _get_header(headers, header_name)
    return bool(provided_secret) and compare_digest(provided_secret, secret)


def _parse_json_body(body: bytes) -> dict[str, Any] | ReleaseWebhookResponse:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ReleaseWebhookResponse(status=400, body="Invalid JSON body")

    if not isinstance(payload, dict):
        return ReleaseWebhookResponse(status=400, body="JSON body must be an object")
    return payload


def _handle_extra_webhook_route(
    *,
    route: ExtraWebhookRoute,
    body: bytes,
    headers: Any,
) -> ReleaseWebhookResponse:
    if not _has_valid_named_secret(
        headers,
        secret=route.secret,
        header_name=route.secret_header,
    ):
        return FORBIDDEN_RESPONSE

    payload = _parse_json_body(body)
    if isinstance(payload, ReleaseWebhookResponse):
        return payload

    try:
        parsed_payload = route.parse_payload(payload)
    except (KeyError, TypeError, ValueError) as error:
        return ReleaseWebhookResponse(status=400, body=str(error))

    route.submit_payload(parsed_payload)
    return ReleaseWebhookResponse(status=202, body=route.accepted_body)


def handle_release_webhook_post(
    *,
    path: str,
    body: bytes,
    headers: Any,
    expected_path: str,
    secret: str | None,
    submit_payload: Callable[[dict[str, Any]], None],
) -> ReleaseWebhookResponse:
    extra_route = _extra_routes.get(path)
    if extra_route is not None:
        return _handle_extra_webhook_route(
            route=extra_route,
            body=body,
            headers=headers,
        )

    if not _has_valid_secret(headers, secret):
        return FORBIDDEN_RESPONSE

    if path != expected_path:
        return ReleaseWebhookResponse(status=404, body="Not found")

    payload = _parse_json_body(body)
    if isinstance(payload, ReleaseWebhookResponse):
        return payload

    try:
        parse_release_candidate_payload(payload)
    except ReleaseCandidateError as error:
        return ReleaseWebhookResponse(status=400, body=str(error))

    submit_payload(payload)
    return ReleaseWebhookResponse(status=202, body="Release candidate queued")


class ReleaseWebhookServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        path: str,
        secret: str | None,
        loop: asyncio.AbstractEventLoop,
        on_payload: Callable[[dict[str, Any]], Awaitable[None]],
    ):
        if not secret:
            raise ValueError("Release webhook secret is required")
        self.host = host
        self.port = port
        self.path = path
        self.secret = secret
        self.loop = loop
        self.on_payload = on_payload
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _send_release_response(self, response: ReleaseWebhookResponse) -> None:
                self.send_response(response.status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(response.body.encode("utf-8"))

            def do_POST(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(content_length)
                response = handle_release_webhook_post(
                    path=self.path,
                    body=body,
                    headers=self.headers,
                    expected_path=owner.path,
                    secret=owner.secret,
                    submit_payload=owner._submit_payload,
                )
                self._send_release_response(response)

            def _reject_unsupported_method(self) -> None:
                self._send_release_response(FORBIDDEN_RESPONSE)

            do_GET = _reject_unsupported_method
            do_HEAD = _reject_unsupported_method
            do_PUT = _reject_unsupported_method
            do_PATCH = _reject_unsupported_method
            do_DELETE = _reject_unsupported_method
            do_OPTIONS = _reject_unsupported_method

            def log_message(self, format: str, *args: object) -> None:
                log.info("Release webhook: " + format, *args)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="dmz-release-webhook",
            daemon=True,
        )
        self._thread.start()
        log.info("Release webhook server listening on %s:%s%s", self.host, self.port, self.path)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _submit_payload(self, payload: dict[str, Any]) -> None:
        future = asyncio.run_coroutine_threadsafe(self.on_payload(payload), self.loop)

        def log_result(done_future: asyncio.Future[None]) -> None:
            try:
                done_future.result()
            except Exception:
                log.exception("Release webhook payload handling failed")

        future.add_done_callback(log_result)
