"""Re-scores an extracted log corpus zip against the current log parser.

Usage: PYTHONPATH=src py scripts/log_corpus_eval.py <extracted corpus dir> [--out report.md]

Corpus layout: <version>/<message id>-<file name> logs plus <same>.json metadata (root_cause = the verdict the bot
posted then, uploader_text, before/after = chat messages). Prints a Markdown table (posted culprit vs. now, hints,
staff replies), totals, and the "Unexplained" logs (a root cause but no culprit and no hint: the next cases to study).
The corpus is untrusted text: it is only read, never executed, and nothing is written into it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bulmaai.utils.log_parser import parse_log


def _culprit(cause: dict | None) -> str:
    if not cause:
        return "-"
    return cause.get("culprit_mod") or cause.get("culprit_jar") or "-"


def _cell(text: object) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def evaluate(root: Path) -> str:
    rows: list[str] = []
    totals = dict.fromkeys(("logs", "root cause", "culprit", "hints", "mod ids", "jars only", "nothing found"), 0)
    unexplained: list[str] = []
    for path in sorted(root.glob("*/*")):
        if path.suffix == ".json" or not path.is_file():
            continue
        meta: dict = {}
        meta_path = path.with_name(path.name + ".json")
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                meta = {}
        report = parse_log(path.read_text(encoding="utf-8", errors="replace"))
        cause = report.root_cause
        now = (cause.culprit_mod or cause.culprit_jar) if cause else None
        staff = sum(1 for m in meta.get("after") or [] if isinstance(m, dict) and m.get("who") == "staff")
        mods = f"{len(report.mods)} ids" if report.mods else (f"{len(report.jars)} jars" if report.jars else "-")
        rows.append(
            f"| {_cell(path.relative_to(root).as_posix())} | {_cell(report.dragonminez_version or '-')} | {mods} "
            f"| {_cell(_culprit(meta.get('root_cause')))} → {_cell(now or '-')} "
            f"| {_cell(cause.exception[:80]) if cause else '-'} "
            f"| {_cell('; '.join(h[:60] for h in report.hints) or '-')} | {staff} |"
        )
        totals["logs"] += 1
        totals["root cause"] += bool(cause)
        totals["culprit"] += bool(now)
        totals["hints"] += bool(report.hints)
        totals["mod ids"] += bool(report.mods)
        totals["jars only"] += bool(report.jars and not report.mods)
        found = cause or report.hints or report.mods or report.jars
        totals["nothing found"] += not found
        if cause and not now and not report.hints:
            unexplained.append(f"- {path.relative_to(root).as_posix()}: {cause.exception[:120]}")
    out = [
        "| file | DMZ | mods | posted → now culprit | now exception | hints | staff replies |",
        "|---|---|---|---|---|---|---|",
        *rows,
        "",
        "## Totals",
        *(f"- {name}: {count}" for name, count in totals.items()),
        "",
        "## Unexplained",
        *(unexplained or ["- none"]),
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = evaluate(args.corpus)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252 and the table has arrows
        print(report)


if __name__ == "__main__":
    main()
