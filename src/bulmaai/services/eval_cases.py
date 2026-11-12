"""The support AI's offline eval set (`eval_cases` table), read by scripts/support_eval.py.

It grows only from staff-vouched sources: a ticket staff marked solved (Auto-close On, or Staff Bulma's solve), an
answer staff wrote on an "Answers to fix" card, and approved support info (pulled in by `support_eval.py build`).
Owner-solved tickets and AI output never make a case. The bot hooks (track_ticket, untrack_ticket, track_feedback)
run in the background through spawn() and never raise into the click."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Coroutine, Sequence

from bulmaai.database.db import get_pool
from bulmaai.utils.language import detect_language_from_text

log = logging.getLogger(__name__)

SOURCES = ("ticket", "support_info", "feedback", "manual")
NO_RESOLUTION = "no resolution reached"

_COLUMNS = (
    "id, source, source_ref, question, reference, language, category, expected_docs, exclude_docs, labelled_at, "
    "active, created_at"
)
_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class NewCase:
    source: str
    source_ref: str
    question: str
    reference: str
    language: str = "en"
    category: str | None = None
    exclude_docs: tuple[str, ...] = ()
    expected_docs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class EvalCase:
    id: int
    source: str
    source_ref: str
    question: str
    reference: str
    language: str
    category: str | None
    expected_docs: tuple[str, ...] | None
    exclude_docs: tuple[str, ...]
    active: bool


def _clean(text: Any) -> str:
    return str(text or "").strip()


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _language(text: str, fallback: Any = None) -> str:
    """The language the question is written in; the fallback (e.g. the ticket's) only for text too short to tell."""
    if len(text.split()) < 4 and fallback in ("en", "es", "pt"):
        return str(fallback)
    return detect_language_from_text(text)


def ticket_ref(channel_id: int) -> str:
    return f"ticket:{channel_id}"


def case_from_transcript(row: Any) -> NewCase | None:
    """A case from a ticket_transcripts row (plus the ticket's `language` / `category` when joined in): question =
    title + problem, reference = resolution, the ticket's own knowledge doc excluded. None when the summary has no
    problem or no real resolution."""
    title, problem, resolution = (_clean(_value(row, key)) for key in ("title", "problem", "resolution"))
    if not problem or not resolution or resolution.lower().rstrip(".") == NO_RESOLUTION:
        return None
    question = f"{title}\n{problem}" if title and title.lower() not in problem.lower() else problem
    knowledge_filename = _clean(_value(row, "knowledge_filename"))
    return NewCase(
        source="ticket",
        source_ref=ticket_ref(int(_value(row, "channel_id"))),
        question=question,
        reference=resolution,
        language=_language(question, _value(row, "language")),
        category=_clean(_value(row, "category")) or None,
        exclude_docs=(knowledge_filename,) if knowledge_filename else (),
    )


def case_from_support_info(entry: Any) -> NewCase | None:
    """A case from an approved support info entry: question = title, reference = body. Support info lives in the
    developer message, not in knowledge_docs, so it is labelled at once with no expected docs (left out of the
    retrieval eval)."""
    title, body = _clean(getattr(entry, "title", "")), _clean(getattr(entry, "body", ""))
    if not title or not body:
        return None
    return NewCase(
        source="support_info",
        source_ref=f"support_info:{entry.id}",
        question=title,
        reference=body,
        language=_language(title),
        expected_docs=(),
    )


def case_from_feedback(message_id: int, question: str, answer: str, language: Any = None) -> NewCase | None:
    """A case from an "Answers to fix" answer: the requester's full question, staff's answer as the reference."""
    question, answer = _clean(question), _clean(answer)
    if not question or not answer:
        return None
    return NewCase(
        source="feedback",
        source_ref=f"feedback:{message_id}",
        question=question,
        reference=answer,
        language=str(language) if language in ("en", "es", "pt") else _language(question),
    )


def _case(row: Any) -> EvalCase:
    expected = _value(row, "expected_docs")
    return EvalCase(
        id=int(row["id"]),
        source=str(row["source"]),
        source_ref=str(row["source_ref"]),
        question=str(row["question"]),
        reference=str(row["reference"]),
        language=str(row["language"] or "en"),
        category=row["category"],
        expected_docs=None if expected is None else tuple(expected),
        exclude_docs=tuple(row["exclude_docs"] or ()),
        active=bool(row["active"]),
    )


ADD_SQL = """
INSERT INTO eval_cases (source, source_ref, question, reference, language, category, exclude_docs, expected_docs,
                        labelled_at, active)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, CASE WHEN $8::text[] IS NULL THEN NULL ELSE now() END, TRUE)
ON CONFLICT (source_ref) DO UPDATE SET
    source = EXCLUDED.source,
    question = EXCLUDED.question,
    reference = EXCLUDED.reference,
    language = EXCLUDED.language,
    category = EXCLUDED.category,
    exclude_docs = EXCLUDED.exclude_docs,
    active = TRUE,
    expected_docs = CASE
        WHEN (eval_cases.question, eval_cases.reference) IS DISTINCT FROM (EXCLUDED.question, EXCLUDED.reference)
            THEN EXCLUDED.expected_docs
        ELSE COALESCE(eval_cases.expected_docs, EXCLUDED.expected_docs) END,
    labelled_at = CASE
        WHEN (eval_cases.question, eval_cases.reference) IS DISTINCT FROM (EXCLUDED.question, EXCLUDED.reference)
            THEN EXCLUDED.labelled_at
        ELSE COALESCE(eval_cases.labelled_at, EXCLUDED.labelled_at) END
RETURNING id
"""


async def add_case(case: NewCase, *, pool: Any | None = None) -> int:
    """Insert or refresh by source_ref (re-activating it). A changed question or reference resets the labels."""
    resolved_pool = pool or await get_pool()
    return int(
        await resolved_pool.fetchval(
            ADD_SQL,
            case.source,
            case.source_ref,
            case.question,
            case.reference,
            case.language,
            case.category,
            list(case.exclude_docs),
            None if case.expected_docs is None else list(case.expected_docs),
        )
    )


async def set_active(source_ref: str, active: bool, *, pool: Any | None = None) -> bool:
    resolved_pool = pool or await get_pool()
    status = await resolved_pool.execute(
        "UPDATE eval_cases SET active = $2 WHERE source_ref = $1 AND active IS DISTINCT FROM $2", source_ref, active
    )
    return str(status).endswith(" 1")


async def deactivate_missing(source: str, keep_refs: Sequence[str], *, pool: Any | None = None) -> int:
    """Deactivates a source's cases whose origin is gone (e.g. support info no longer active)."""
    resolved_pool = pool or await get_pool()
    status = await resolved_pool.execute(
        "UPDATE eval_cases SET active = FALSE WHERE source = $1 AND active AND NOT (source_ref = ANY($2::text[]))",
        source,
        list(keep_refs),
    )
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


async def set_labels(case_id: int, docs: Sequence[str], *, pool: Any | None = None) -> None:
    resolved_pool = pool or await get_pool()
    await resolved_pool.execute(
        "UPDATE eval_cases SET expected_docs = $2, labelled_at = now() WHERE id = $1", case_id, list(docs)
    )


async def list_cases(
    *,
    source: str | None = None,
    active: bool | None = True,
    labelled: bool | None = None,
    with_docs: bool = False,
    limit: int | None = None,
    pool: Any | None = None,
) -> list[EvalCase]:
    """Cases by id. labelled: True/False filters on expected_docs being set; with_docs: only non-empty labels."""
    where, args = [], []
    if source:
        args.append(source)
        where.append(f"source = ${len(args)}")
    if active is not None:
        args.append(active)
        where.append(f"active = ${len(args)}")
    if labelled is True or with_docs:
        where.append("expected_docs IS NOT NULL")
    elif labelled is False:
        where.append("expected_docs IS NULL")
    if with_docs:
        where.append("cardinality(expected_docs) > 0")
    sql = f"SELECT {_COLUMNS} FROM eval_cases"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit:
        args.append(int(limit))
        sql += f" LIMIT ${len(args)}"
    resolved_pool = pool or await get_pool()
    return [_case(row) for row in await resolved_pool.fetch(sql, *args)]


TRANSCRIPT_SQL = """
SELECT tt.channel_id, tt.title, tt.problem, tt.resolution, tt.knowledge_filename, t.language, t.category
FROM ticket_transcripts tt
LEFT JOIN LATERAL (
    SELECT language, category FROM tickets WHERE tickets.channel_id = tt.channel_id ORDER BY ticket_id DESC LIMIT 1
) t ON TRUE
"""


async def resolved_transcripts(*, pool: Any | None = None) -> list[Any]:
    """The backfill: every channel's latest transcript marked solved and closed by someone other than its owner (owner
    solves and bot inactivity closes, closer NULL, are left out)."""
    resolved_pool = pool or await get_pool()
    return await resolved_pool.fetch(
        TRANSCRIPT_SQL
        + """
        WHERE tt.id IN (SELECT max(id) FROM ticket_transcripts GROUP BY channel_id)
          AND tt.resolved AND tt.closed_by_id IS NOT NULL AND tt.closed_by_id IS DISTINCT FROM tt.requester_id
          AND coalesce(tt.problem, '') <> '' AND coalesce(tt.resolution, '') <> ''
        ORDER BY tt.id
        """
    )


# ---- bot hooks (background, never raise) ---------------------------------------------------------------------------


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Runs a hook in the background, so the click never waits on it."""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def track_ticket(channel_id: int, *, pool: Any | None = None) -> None:
    """Staff vouched for the ticket as solved: a case from its latest transcript."""
    try:
        resolved_pool = pool or await get_pool()
        row = await resolved_pool.fetchrow(
            TRANSCRIPT_SQL + " WHERE tt.id = (SELECT max(id) FROM ticket_transcripts WHERE channel_id = $1)",
            channel_id,
        )
        case = case_from_transcript(row) if row is not None else None
        if case is not None:
            await add_case(case, pool=resolved_pool)
    except Exception:
        log.exception("Could not add the ticket's eval case", extra={"channel_id": channel_id})


async def untrack_ticket(channel_id: int, *, pool: Any | None = None) -> None:
    """Staff took the solved mark back (Auto-close Off): its case stops counting."""
    try:
        await set_active(ticket_ref(channel_id), False, pool=pool)
    except Exception:
        log.exception("Could not deactivate the ticket's eval case", extra={"channel_id": channel_id})


async def track_feedback(message_id: int, answer: str) -> None:
    """Staff wrote the right answer to a 👎 answer: a case of the requester's question and that answer."""
    try:
        from bulmaai.services import ai_feedback

        row = await ai_feedback.get(message_id)
        fixed = await ai_feedback.answer_to_fix(row) if row is not None else None
        case = case_from_feedback(message_id, fixed.question, answer, row.language) if fixed is not None else None
        if case is not None:
            await add_case(case)
    except Exception:
        log.exception("Could not add the feedback eval case", extra={"message_id": message_id})
