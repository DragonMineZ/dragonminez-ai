import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bulmaai.database.db import close_db_pool, init_db_pool
from bulmaai.services.support_traces import (
    list_support_eval_trace_rows,
    support_trace_to_eval_row,
    write_eval_jsonl,
)


async def _export(*, output: Path, limit: int) -> int:
    load_dotenv()
    await init_db_pool()
    try:
        rows = await list_support_eval_trace_rows(limit=limit)
        eval_rows = [support_trace_to_eval_row(row) for row in rows]
        write_eval_jsonl(eval_rows, output)
        return len(eval_rows)
    finally:
        await close_db_pool()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export DragonMineZ support traces as eval-ready JSONL."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/evals/support_eval_dataset.jsonl"),
        help="Path to write JSONL rows.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum number of recent support traces to export.",
    )
    args = parser.parse_args()

    count = asyncio.run(_export(output=args.output, limit=args.limit))
    print(f"Exported {count} support eval rows to {args.output}")


if __name__ == "__main__":
    main()
