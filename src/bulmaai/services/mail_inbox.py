"""One mailbox (MAIL_USER on Hostinger) mirrored into an admin channel (cogs/mail_inbox.py, table mail_messages).
New mail is read over IMAP without marking it read or moving it, parsed with the stdlib email package and posted as a
card (ui/mail_cards.py); an admin's reply is claimed on the row (WHERE replied_at IS NULL, first wins) and sent over
SMTP from the same address. IMAP and SMTP calls are blocking: run them in a thread."""

import hashlib
import imaplib
import json
import logging
import re
import smtplib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from bulmaai.database.db import get_pool

log = logging.getLogger(__name__)

IMAP_PORT = 993
SMTP_PORT = 465
TIMEOUT = 30
FETCH_LIMIT = 25
BODY_MAX = 4000
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Incoming:
    message_id: str
    from_addr: str
    from_name: str = ""
    subject: str = ""
    body: str = ""
    attachments: list[str] = field(default_factory=list)
    received_at: datetime | None = None


@dataclass(frozen=True)
class Mail:
    id: int
    message_id: str
    from_addr: str
    from_name: str = ""
    subject: str = ""
    body: str = ""
    attachments: list[str] = field(default_factory=list)
    received_at: datetime | None = None
    created_at: datetime | None = None
    card_channel_id: int | None = None
    card_message_id: int | None = None
    replied_by: int | None = None
    replied_at: datetime | None = None
    reply_text: str | None = None


def _mail(row: Any) -> Mail | None:
    if row is None:
        return None
    attachments = row["attachments"]
    if isinstance(attachments, str):
        attachments = json.loads(attachments or "[]")
    return Mail(
        id=row["id"],
        message_id=row["message_id"],
        from_addr=row["from_addr"],
        from_name=row["from_name"] or "",
        subject=row["subject"] or "",
        body=row["body"] or "",
        attachments=[str(name) for name in attachments or []],
        received_at=row["received_at"],
        created_at=row["created_at"],
        card_channel_id=row["card_channel_id"],
        card_message_id=row["card_message_id"],
        replied_by=row["replied_by"],
        replied_at=row["replied_at"],
        reply_text=row["reply_text"],
    )


def enabled(settings) -> bool:
    return bool(
        getattr(settings, "mail_channel_id", None)
        and getattr(settings, "mail_user", None)
        and getattr(settings, "mail_password", None)
    )


# --- parsing ---------------------------------------------------------------------------------------------------------


class _HTMLText(HTMLParser):
    BLOCKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(self._skip - 1, 0)
        elif tag in self.BLOCKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


def clean_body(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:BODY_MAX]


def _content(part) -> str:
    try:
        return str(part.get_content())
    except Exception:  # unknown charset or broken encoding: read what we can
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)


def _body(message) -> str:
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        return clean_body(_content(plain))
    html = message.get_body(preferencelist=("html",))
    return clean_body(html_to_text(_content(html))) if html is not None else ""


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]  # no CR/LF: these end up in headers of the reply


def mail_key(message_id: Any, from_header: Any, date_header: Any, subject: Any) -> str:
    """The Message-ID header, or a stable stand-in for mail that has none."""
    real = _one_line(message_id, 500)
    if real:
        return real
    digest = hashlib.sha256(f"{from_header or ''}\n{date_header or ''}\n{subject or ''}".encode("utf-8", "replace"))
    return "sha256:" + digest.hexdigest()


def parse_message(raw: bytes) -> Incoming | None:
    """A raw RFC 822 message as stored fields; None when it can't be read or has no sender address."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        from_header = str(message.get("From", "") or "")
        name, addr = parseaddr(from_header)
        addr = _one_line(addr, 320)
        if "@" not in addr:
            return None
        subject = _one_line(message.get("Subject", ""), 300)
        date_header = str(message.get("Date", "") or "")
        received_at = None
        if date_header:
            try:
                received_at = parsedate_to_datetime(date_header)
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=timezone.utc)
            except Exception:
                received_at = None
        attachments = [
            _one_line(part.get_filename(), 200) for part in message.iter_attachments() if part.get_filename()
        ][:20]
        return Incoming(
            message_id=mail_key(message.get("Message-ID"), from_header, date_header, subject),
            from_addr=addr,
            from_name=_one_line(name, 200),
            subject=subject,
            body=_body(message),
            attachments=attachments,
            received_at=received_at,
        )
    except Exception as error:
        log.info("Skipped a mail that couldn't be parsed: %s", type(error).__name__)
        return None


# --- IMAP ------------------------------------------------------------------------------------------------------------


def imap_date(day: date) -> str:
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"  # locale-free %d-%b-%Y


def _header_fields(raw: bytes) -> tuple[str, str]:
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return _one_line(message.get("Message-ID"), 500), parseaddr(str(message.get("From", "") or ""))[1].lower()


def fetch_new(host: str, user: str, password: str, known: set[str], limit: int = FETCH_LIMIT) -> list[bytes]:
    """Raw messages of the newest mail since yesterday (oldest first), skipping mail already stored (by Message-ID)
    and mail sent by the mailbox itself. Read-only: BODY.PEEK never sets \\Seen, nothing is moved or deleted."""
    imap = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=TIMEOUT)
    try:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        since = imap_date(datetime.now(timezone.utc).date() - timedelta(days=1))
        _typ, data = imap.search(None, "SINCE", since)
        numbers = (data[0] or b"").split()[-limit:] if data else []
        out: list[bytes] = []
        for number in numbers:
            _typ, head = imap.fetch(number, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)])")
            raw_head = next((part[1] for part in head or [] if isinstance(part, tuple)), b"")
            message_id, sender = _header_fields(raw_head)
            if (message_id and message_id in known) or sender == user.lower():
                continue
            _typ, parts = imap.fetch(number, "(BODY.PEEK[])")
            out += [part[1] for part in parts or [] if isinstance(part, tuple)]
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# --- SMTP ------------------------------------------------------------------------------------------------------------


def build_reply(mail: Any, mail_user: str, text: str) -> EmailMessage:
    subject = _one_line(mail.subject, 300)
    message = EmailMessage()
    message["From"] = mail_user
    message["To"] = _one_line(mail.from_addr, 320)
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}".rstrip()
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=mail_user.rpartition("@")[2] or None)
    if mail.message_id.startswith("<"):
        message["In-Reply-To"] = mail.message_id
        message["References"] = mail.message_id
    message.set_content(text)
    return message


def send_reply(host: str, user: str, password: str, message: EmailMessage) -> None:
    with smtplib.SMTP_SSL(host, SMTP_PORT, timeout=TIMEOUT) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)


# --- storage ---------------------------------------------------------------------------------------------------------


async def known_ids(days: int = 3) -> set[str]:
    """Message-IDs stored lately: the poller skips downloading these again."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT message_id FROM mail_messages WHERE created_at > now() - make_interval(days => $1)", days
    )
    return {row["message_id"] for row in rows}


async def insert(incoming: Incoming) -> Mail | None:
    """None when this Message-ID is already stored."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO mail_messages (message_id, from_addr, from_name, subject, body, attachments, received_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING *
        """,
        incoming.message_id,
        incoming.from_addr,
        incoming.from_name,
        incoming.subject,
        incoming.body[:BODY_MAX],
        json.dumps(incoming.attachments),
        incoming.received_at,
    )
    return _mail(row)


async def get(mail_id: int) -> Mail | None:
    pool = await get_pool()
    return _mail(await pool.fetchrow("SELECT * FROM mail_messages WHERE id = $1", mail_id))


async def delete(mail_id: int) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM mail_messages WHERE id = $1", mail_id)


async def set_card(mail_id: int, channel_id: int, message_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE mail_messages SET card_channel_id = $2, card_message_id = $3 WHERE id = $1",
        mail_id,
        channel_id,
        message_id,
    )


async def claim(mail_id: int, staff_id: int, text: str) -> Mail | None:
    """First reply wins: None when someone already replied (or the mail is gone)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE mail_messages SET replied_by = $2, replied_at = now(), reply_text = $3
        WHERE id = $1 AND replied_at IS NULL
        RETURNING *
        """,
        mail_id,
        staff_id,
        text,
    )
    return _mail(row)


async def release(mail_id: int, staff_id: int) -> None:
    """Gives back a claim whose send failed (only the replier's own)."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE mail_messages SET replied_by = NULL, replied_at = NULL, reply_text = NULL WHERE id = $1 AND replied_by = $2",
        mail_id,
        staff_id,
    )
