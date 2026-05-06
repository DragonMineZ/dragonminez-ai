import asyncio
import logging
import re
import traceback
from dataclasses import dataclass
from typing import Any

import discord


SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+[\w.\-]+"),
    re.compile(r"(?i)\b(discord_token|openai_key|gh_app_private_key_pem|patreon_creator_token|curseforge_api_key|token|api[_-]?key)\s*=\s*[^,\s]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
)
FORWARDED_CONTEXT_FIELDS = (
    "event",
    "guild_id",
    "channel_id",
    "message_id",
    "user_id",
    "command",
    "task",
    "exception_type",
)
MAX_DESCRIPTION_CHARS = 1800
MAX_EMBED_FIELDS = 25
MAX_FIELD_VALUE_CHARS = 200
MAX_TRACEBACK_CHARS = 900
_CONTROL_EXTRA_FIELDS = {"discord_forward", "suppress_discord_forward"}
_SENSITIVE_EXTRA_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "webhook",
    "content",
    "prompt",
    "response",
    "body",
    "text",
)
_STANDARD_LOG_RECORD_ATTRS = set(
    logging.LogRecord(
        name="",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {"asctime", "message"}


@dataclass(frozen=True)
class LogEmbedPayload:
    title: str
    description: str
    color: int
    fields: dict[str, str]
    traceback_text: str | None = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def sanitize_log_text(text: str) -> str:
    sanitized = text
    for pattern in SENSITIVE_TEXT_PATTERNS:
        sanitized = pattern.sub(lambda match: f"{match.group(1)} [redacted]" if match.lastindex else "[redacted]", sanitized)
    return sanitized


def should_forward_record(record: logging.LogRecord, *, min_level: int = logging.WARNING) -> bool:
    if getattr(record, "suppress_discord_forward", False):
        return False
    if record.name.startswith(__name__):
        return False
    return bool(getattr(record, "discord_forward", False)) or record.levelno >= min_level


def _level_color(levelno: int) -> int:
    if levelno >= logging.CRITICAL:
        return 0x992D22
    if levelno >= logging.ERROR:
        return 0xE74C3C
    if levelno >= logging.WARNING:
        return 0xF1C40F
    return 0x3498DB


def _record_traceback(record: logging.LogRecord) -> str | None:
    if not record.exc_info:
        return None
    text = "".join(traceback.format_exception(*record.exc_info))
    return _truncate(sanitize_log_text(text), MAX_TRACEBACK_CHARS)


def _is_sensitive_extra_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized == "id" or normalized.endswith("_id"):
        return False
    return any(part in normalized for part in _SENSITIVE_EXTRA_KEY_PARTS)


def _safe_extra_fields(record: logging.LogRecord) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for key, value in record.__dict__.items():
        if key in _STANDARD_LOG_RECORD_ATTRS or key in _CONTROL_EXTRA_FIELDS or key.startswith("_"):
            continue
        if _is_sensitive_extra_key(key):
            continue
        fields.append((key, _truncate(sanitize_log_text(str(value)), MAX_FIELD_VALUE_CHARS)))
    return sorted(fields, key=lambda item: item[0])


def build_log_embed_payload(record: logging.LogRecord) -> LogEmbedPayload:
    fields: dict[str, str] = {}
    for field_name in FORWARDED_CONTEXT_FIELDS:
        value = getattr(record, field_name, None)
        if value is None:
            continue
        fields[field_name] = _truncate(sanitize_log_text(str(value)), MAX_FIELD_VALUE_CHARS)

    for field_name, value in _safe_extra_fields(record):
        if len(fields) >= MAX_EMBED_FIELDS:
            break
        fields.setdefault(field_name, value)

    if record.exc_info and "exception_type" not in fields:
        exception = record.exc_info[1]
        if exception is not None:
            fields["exception_type"] = type(exception).__name__

    message = sanitize_log_text(record.getMessage())
    return LogEmbedPayload(
        title=f"{record.levelname} | {record.name}",
        description=_truncate(message or "(no message)", MAX_DESCRIPTION_CHARS),
        color=_level_color(record.levelno),
        fields=fields,
        traceback_text=_record_traceback(record),
    )


def payload_to_embed(payload: LogEmbedPayload) -> discord.Embed:
    embed = discord.Embed(
        title=_truncate(payload.title, 250),
        description=payload.description,
        color=discord.Color(payload.color),
        timestamp=discord.utils.utcnow(),
    )
    for name, value in payload.fields.items():
        embed.add_field(name=name, value=value or "-", inline=True)
    if payload.traceback_text:
        embed.add_field(
            name="Traceback",
            value=f"```py\n{payload.traceback_text}\n```",
            inline=False,
        )
    return embed


class DiscordLogHandler(logging.Handler):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[LogEmbedPayload],
        min_level: int,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._loop = loop
        self._queue = queue
        self._min_level = min_level

    def emit(self, record: logging.LogRecord) -> None:
        if not should_forward_record(record, min_level=self._min_level):
            return
        try:
            payload = build_log_embed_payload(record)
            self._loop.call_soon_threadsafe(self._enqueue_payload, payload)
        except Exception:
            self.handleError(record)

    def _enqueue_payload(self, payload: LogEmbedPayload) -> None:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            return


class DiscordLogForwardingQueue:
    def __init__(
        self,
        sender,
        *,
        max_queue_size: int = 100,
        min_level: int = logging.WARNING,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        self._sender = sender
        self._queue: asyncio.Queue[LogEmbedPayload] = asyncio.Queue(maxsize=max_queue_size)
        self._min_level = min_level
        self._task: asyncio.Task[None] | None = None
        self.dropped_count = 0
        self.send_error_count = 0

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._send_loop(), name="discord-log-forwarding-queue")

    async def stop(self, *, drain: bool = True) -> None:
        if drain:
            await self.flush()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def flush(self) -> None:
        await self._queue.join()

    def enqueue(self, record: logging.LogRecord) -> bool:
        if not should_forward_record(record, min_level=self._min_level):
            return False
        try:
            self._queue.put_nowait(build_log_embed_payload(record))
        except asyncio.QueueFull:
            self.dropped_count += 1
            return False
        return True

    async def _send_loop(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._sender(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.send_error_count += 1
            finally:
                self._queue.task_done()


class DiscordLogForwarder:
    def __init__(
        self,
        *,
        bot: discord.Client,
        channel_id: int,
        min_level: int = logging.WARNING,
        queue_size: int = 100,
    ) -> None:
        self._bot = bot
        self._channel_id = channel_id
        self._queue: asyncio.Queue[LogEmbedPayload] = asyncio.Queue(maxsize=queue_size)
        self._handler = DiscordLogHandler(
            loop=asyncio.get_running_loop(),
            queue=self._queue,
            min_level=min_level,
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def handler(self) -> DiscordLogHandler:
        return self._handler

    def start(self) -> None:
        root = logging.getLogger()
        if self._handler not in root.handlers:
            root.addHandler(self._handler)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._send_loop(), name="discord-log-forwarder")

    async def stop(self) -> None:
        logging.getLogger().removeHandler(self._handler)
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _resolve_channel(self) -> Any:
        channel = self._bot.get_channel(self._channel_id)
        if channel is None:
            channel = await self._bot.fetch_channel(self._channel_id)
        return channel if hasattr(channel, "send") else None

    async def _send_loop(self) -> None:
        await self._bot.wait_until_ready()
        while True:
            payload = await self._queue.get()
            try:
                channel = await self._resolve_channel()
                if channel is not None:
                    await channel.send(
                        embed=payload_to_embed(payload),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self._queue.task_done()


def parse_log_level(level_name: str | None, default: int = logging.WARNING) -> int:
    if not level_name:
        return default
    value = getattr(logging, level_name.upper(), None)
    return value if isinstance(value, int) else default


def install_discord_log_forwarder(
    *,
    bot: discord.Client,
    channel_id: int,
    min_level_name: str | None,
) -> DiscordLogForwarder:
    forwarder = DiscordLogForwarder(
        bot=bot,
        channel_id=channel_id,
        min_level=parse_log_level(min_level_name),
    )
    forwarder.start()
    return forwarder
