import logging
from dataclasses import dataclass
from typing import Any

import discord

from bulmaai.config import load_settings
from bulmaai.database.db import get_pool
from bulmaai.services.docs_ingestion import ingest_bytes
from bulmaai.utils.language import detect_language_from_text
from bulmaai.utils.permissions import is_staff

log = logging.getLogger(__name__)

SOURCE_PREFIX = "closed-ticket"
CLOSED_TICKET_HISTORY_LIMIT = 200
MIN_REQUESTER_CHARS = 20
MIN_RESOLUTION_CHARS = 20


@dataclass(slots=True)
class TicketKnowledgeDocument:
    source: str
    lang: str
    content: str


def _message_text(message: discord.Message) -> str:
    content = message.clean_content.strip()
    attachment_lines = [f"[Attachment] {attachment.filename}" for attachment in message.attachments]
    if attachment_lines:
        content = f"{content}\n" if content else ""
        content += "\n".join(attachment_lines)
    return content.strip()


def _find_requester_id(messages: list[discord.Message], bot_user_id: int | None) -> int | None:
    for message in messages:
        if message.author.bot:
            continue
        if bot_user_id is not None and message.author.id == bot_user_id:
            continue
        if is_staff(message.author):  # type: ignore[arg-type]
            continue
        return message.author.id
    return None


def _build_ticket_document(
    *,
    channel: discord.TextChannel,
    messages: list[discord.Message],
    bot_user_id: int | None,
) -> TicketKnowledgeDocument | None:
    requester_id = _find_requester_id(messages, bot_user_id)
    if requester_id is None:
        return None

    requester_lines: list[str] = []
    staff_lines: list[str] = []

    for message in messages:
        if message.author.bot:
            continue
        if bot_user_id is not None and message.author.id == bot_user_id:
            continue

        content = _message_text(message)
        if not content:
            continue

        if message.author.id == requester_id:
            requester_lines.append(content)
            continue

        if is_staff(message.author):  # type: ignore[arg-type]
            helper_name = getattr(message.author, "display_name", message.author.name)
            staff_lines.append(f"{helper_name}: {content}")
            continue

    requester_text = "\n".join(requester_lines).strip()
    staff_text = "\n".join(staff_lines).strip()
    if len(requester_text) < MIN_REQUESTER_CHARS or len(staff_text) < MIN_RESOLUTION_CHARS:
        return None

    lang = detect_language_from_text(requester_text)
    content = (
        f"# Resolved Ticket: {channel.name}\n\n"
        "## User problem\n"
        f"{requester_text}\n\n"
        "## Staff resolution\n"
        f"{staff_text}\n"
    )
    return TicketKnowledgeDocument(
        source=f"{SOURCE_PREFIX}/{channel.id}",
        lang=lang,
        content=content,
    )


async def _fetch_ticket_sync_state() -> dict[int, int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT channel_id, last_message_id
            FROM ticket_knowledge_sync_state
            """
        )
    return {int(row["channel_id"]): int(row["last_message_id"]) for row in rows}


async def _store_ticket_sync_state(
    *,
    channel_id: int,
    category_id: int,
    source: str,
    lang: str | None,
    last_message_id: int,
    message_count: int,
    indexed: bool,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ticket_knowledge_sync_state (
                channel_id,
                category_id,
                source,
                lang,
                last_message_id,
                message_count,
                indexed,
                created_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
            ON CONFLICT (channel_id)
            DO UPDATE SET
                category_id = EXCLUDED.category_id,
                source = EXCLUDED.source,
                lang = EXCLUDED.lang,
                last_message_id = EXCLUDED.last_message_id,
                message_count = EXCLUDED.message_count,
                indexed = EXCLUDED.indexed,
                updated_at = now()
            """,
            channel_id,
            category_id,
            source,
            lang,
            last_message_id,
            message_count,
            indexed,
        )


async def sync_closed_ticket_knowledge(bot: discord.Client) -> dict[str, Any]:
    settings = load_settings()
    if not settings.ai_closed_ticket_category_ids:
        return {"scanned": 0, "indexed": 0, "skipped": 0}

    synced_channels = await _fetch_ticket_sync_state()
    scanned = 0
    indexed = 0
    skipped = 0
    bot_user_id = getattr(bot.user, "id", None)

    for category_id in settings.ai_closed_ticket_category_ids:
        category = bot.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            log.warning("Closed ticket category %s is not available to the bot", category_id)
            continue

        for channel in category.text_channels:
            scanned += 1
            last_message_id = int(channel.last_message_id or 0)
            if not last_message_id:
                skipped += 1
                continue
            if synced_channels.get(channel.id) == last_message_id:
                continue

            messages = [
                message
                async for message in channel.history(
                    limit=CLOSED_TICKET_HISTORY_LIMIT,
                    oldest_first=True,
                )
            ]
            document = _build_ticket_document(
                channel=channel,
                messages=messages,
                bot_user_id=bot_user_id,
            )
            source = f"{SOURCE_PREFIX}/{channel.id}"
            if document is None:
                skipped += 1
                await _store_ticket_sync_state(
                    channel_id=channel.id,
                    category_id=category.id,
                    source=source,
                    lang=None,
                    last_message_id=last_message_id,
                    message_count=len(messages),
                    indexed=False,
                )
                continue

            await ingest_bytes(
                data=document.content.encode("utf-8"),
                filename=f"{channel.id}.md",
                lang=document.lang,
                source=document.source,
                source_type="ticket_solution",
                replace=True,
            )
            indexed += 1
            await _store_ticket_sync_state(
                channel_id=channel.id,
                category_id=category.id,
                source=document.source,
                lang=document.lang,
                last_message_id=last_message_id,
                message_count=len(messages),
                indexed=True,
            )

    return {
        "scanned": scanned,
        "indexed": indexed,
        "skipped": skipped,
    }
