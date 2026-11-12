"""Offline evals of the support AI, run from the repo root on the VPS (needs the bot's .env and Postgres).

  py scripts/support_eval.py build                    # backfill cases (staff-solved tickets, approved support info)
  py scripts/support_eval.py label [--relabel] [--limit N]
  py scripts/support_eval.py retrieval [--k 5]        # no AI
  py scripts/support_eval.py answers [--limit N] [--source S] [--model M]
  py scripts/support_eval.py report [--kind retrieval|answers]

Runs are written to data/evals/runs/<UTC stamp>-<kind>.json (gitignored) and diffed against the previous run."""

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

load_dotenv()

from bulmaai.config import load_settings  # noqa: E402
from bulmaai.database.db import close_db_pool, init_db_pool  # noqa: E402
from bulmaai.services import ai_usage, eval_cases, evals, knowledge, support_info  # noqa: E402

RUNS_DIR = REPO_ROOT / evals.RUNS_DIR


def _cost() -> float:
    return sum(float(row[8] or 0) for row in ai_usage.pending())


async def _finish_ai(started_cost: float = 0.0) -> None:
    spent = _cost() - started_cost
    written = await ai_usage.flush()
    print(f"AI cost of this run: ${spent:.4f} ({written} calls recorded in ai_usage)")


async def build() -> None:
    counts: Counter[str] = Counter()
    for row in await eval_cases.resolved_transcripts():
        case = eval_cases.case_from_transcript(row)
        if case is not None:
            await eval_cases.add_case(case)
            counts["ticket"] += 1
    refs = []
    for entry in await support_info.list_active():
        # Feedback entries already have a case with the requester's real question (track_feedback).
        if entry.kind != "permanent" or entry.source == "feedback":
            continue
        case = eval_cases.case_from_support_info(entry)
        if case is not None:
            await eval_cases.add_case(case)
            refs.append(case.source_ref)
            counts["support_info"] += 1
    gone = await eval_cases.deactivate_missing("support_info", refs)
    active = Counter(case.source for case in await eval_cases.list_cases())
    print(f"Upserted: {dict(counts)}; support info cases deactivated: {gone}")
    print(f"Active cases by source: {dict(active)}")


async def label(*, relabel: bool, limit: int | None) -> None:
    from bulmaai.services.ai_client import client

    cases = await eval_cases.list_cases(labelled=None if relabel else False, limit=limit)
    cases = [case for case in cases if case.source != "support_info"]
    started = _cost()

    async def one(case):
        docs = await evals.label_case(case, search=knowledge.search, client=client)
        await eval_cases.set_labels(case.id, docs)
        return {"id": case.id, "docs": docs}

    results = await evals.gather_limited(cases, one)
    failed = [row for row in results if row.get("error")]
    gaps = sum(1 for row in results if not row.get("error") and not row["docs"])
    print(f"Labelled {len(results) - len(failed)} cases ({gaps} with no covering doc), {len(failed)} failed")
    for row in failed:
        print(f"  #{row['id']}: {row['error']}")
    await _finish_ai(started)


def _report(run: dict, path: Path) -> None:
    previous = [p for p in evals.run_files(run["kind"], RUNS_DIR) if p.name < path.name]
    diff = evals.diff_runs(evals.load_run(previous[-1]) if previous else None, run)
    print(f"{run['kind']} run {run['created_at']} ({path.name})  config: {run.get('config')}")
    print(evals.format_metrics(run["metrics"]))
    print(evals.format_diff(diff, run.get("cases") or []))


def _save_and_report(kind: str, config: dict, metrics: dict, rows: list, extra: dict | None = None) -> None:
    now = datetime.now(timezone.utc)
    run = {"kind": kind, "created_at": now.isoformat(), "config": config, "metrics": metrics, "cases": rows, **(extra or {})}
    path = evals.save_run(run, directory=RUNS_DIR, now=now)
    _report(run, path)


async def retrieval(*, k: int) -> None:
    labelled = await eval_cases.list_cases(labelled=True)
    cases = [case for case in labelled if case.expected_docs]
    # Support info cases are labelled '{}' by design (they live in the prompt, not in knowledge_docs): not gaps.
    gaps = sum(1 for case in labelled if not case.expected_docs and case.source != "support_info")
    rows = await evals.gather_limited(cases, lambda case: evals.retrieval_case(case, search=knowledge.search, k=k))
    rows = [row for row in rows if not row.get("error")]
    metrics = evals.retrieval_metrics(rows, gaps=gaps)
    misses = [row for row in rows if not row["pass"]]
    _save_and_report("retrieval", {"k": k}, metrics, rows)
    if misses:
        print(f"Misses ({len(misses)}):")
        for row in misses:
            print(f"  #{row['id']} [{row['source']}/{row['language']}] {evals.excerpt(row['question'])}")
            print(f"      expected {row['expected']}  got {row['got']}")


async def answers(*, limit: int | None, source: str | None, model: str | None) -> None:
    from bulmaai.services import ai_client, ai_tools

    settings = load_settings()
    cases = await eval_cases.list_cases(source=source, limit=limit)
    info_text = support_info.render_for_ai(await support_info.list_active())
    evals.record_retrieval()
    started = _cost()
    rows = await evals.gather_limited(
        cases,
        lambda case: evals.answer_case(
            case,
            agent=ai_client.run_support_agent,
            client=ai_client.client,
            tools=ai_tools.LOOKUP_TOOL_NAMES,
            settings=settings,
            model=model,
            support_info_text=info_text,
        ),
    )
    spent = _cost() - started
    config = {"limit": limit, "source": source, "model": model or settings.ai_support_model or settings.ai_model}
    _save_and_report("answers", config, evals.answer_metrics(rows), rows, {"cost_usd": round(spent, 4)})
    for row in rows:
        if row.get("error"):
            print(f"  #{row['id']} error: {row['error']}")
        elif row.get("leaked"):
            print(f"  #{row['id']} leaked excluded docs: {row['leaked']}")
    await _finish_ai(started)


def report(kind: str | None) -> None:
    kinds = [kind] if kind else ["retrieval", "answers"]
    for each in kinds:
        files = evals.run_files(each, RUNS_DIR)
        if not files:
            print(f"No {each} runs yet.")
            continue
        _report(evals.load_run(files[-1]), files[-1])


async def _run(args: argparse.Namespace) -> None:
    await init_db_pool()
    try:
        if args.command == "build":
            await build()
        elif args.command == "label":
            await label(relabel=args.relabel, limit=args.limit)
        elif args.command == "retrieval":
            await retrieval(k=args.k)
        elif args.command == "answers":
            await answers(limit=args.limit, source=args.source, model=args.model)
    finally:
        await ai_usage.flush()
        await close_db_pool()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Offline evals of Bulma's support AI.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="Backfill eval cases from staff-vouched sources.")
    label_parser = commands.add_parser("label", help="Label expected knowledge docs with Haiku.")
    label_parser.add_argument("--relabel", action="store_true", help="Label every active case again.")
    label_parser.add_argument("--limit", type=int)
    retrieval_parser = commands.add_parser("retrieval", help="Score knowledge.search (no AI).")
    retrieval_parser.add_argument("--k", type=int, default=5)
    answers_parser = commands.add_parser("answers", help="Replay cases through the support AI and grade them.")
    answers_parser.add_argument("--limit", type=int)
    answers_parser.add_argument("--source", choices=eval_cases.SOURCES)
    answers_parser.add_argument("--model", help="First-pass model, e.g. claude-sonnet-5-5.")
    report_parser = commands.add_parser("report", help="Re-print the latest run with its diff.")
    report_parser.add_argument("--kind", choices=("retrieval", "answers"))
    args = parser.parse_args()
    if args.command == "report":
        report(args.kind)
    else:
        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
