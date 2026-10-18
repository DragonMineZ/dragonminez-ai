import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bulmaai.config import load_settings
from bulmaai.database.db import close_db_pool, init_db_pool
from bulmaai.services.support_faq import (
    FAQ_KNOWLEDGE_FILENAME,
    publish_faq_markdown,
    suggest_faq_candidates,
    support_trace_to_faq_source,
    write_faq_markdown,
)
from bulmaai.services.support_traces import list_support_eval_trace_rows


def _repo_relative_path(path: str | Path) -> Path:
    value = Path(path)
    if value.is_absolute():
        return value
    return REPO_ROOT / value


async def _run(
    *,
    output: Path | None,
    limit: int,
    model: str | None,
    max_candidates: int,
    min_confidence: float,
    publish: bool,
    publish_existing: bool,
) -> tuple[int, Path, bool | None]:
    load_dotenv()
    settings = load_settings()
    output_path = _repo_relative_path(output or settings.ai_faq_generated_path)
    suggestion_model = model or settings.ai_faq_suggestion_model

    count = 0
    publish_result = None
    await init_db_pool()
    try:
        if not publish_existing:
            rows = await list_support_eval_trace_rows(limit=limit)
            sources = [support_trace_to_faq_source(row) for row in rows]
            candidates = await suggest_faq_candidates(
                sources,
                model=suggestion_model,
                max_candidates=max_candidates,
                min_confidence=min_confidence,
            )
            write_faq_markdown(candidates, output_path)
            count = len(candidates)
        if publish or publish_existing:
            if not output_path.is_file():
                raise RuntimeError(f"No FAQ Markdown at {output_path}; generate it first.")
            publish_result = await publish_faq_markdown(output_path.read_text(encoding="utf-8"))
    finally:
        await close_db_pool()
    return count, output_path, publish_result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suggest DragonMineZ FAQ entries from support traces and write Markdown."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Markdown path to write. Defaults to AI_FAQ_GENERATED_PATH.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum number of recent support traces to review.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Claude model for candidate generation. Defaults to AI_FAQ_SUGGESTION_MODEL.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum FAQ candidates to request.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.6,
        help="Drop generated candidates below this confidence.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help=f"Generate Markdown, then publish it to the support AI knowledge ({FAQ_KNOWLEDGE_FILENAME}).",
    )
    parser.add_argument(
        "--publish-existing",
        action="store_true",
        help="Publish the existing Markdown file without regenerating it.",
    )
    args = parser.parse_args()

    try:
        count, output_path, publish_result = asyncio.run(
            _run(
                output=args.output,
                limit=args.limit,
                model=args.model,
                max_candidates=args.max_candidates,
                min_confidence=args.min_confidence,
                publish=args.publish,
                publish_existing=args.publish_existing,
            )
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.publish_existing:
        print(f"Publishing existing FAQ Markdown from {output_path}")
    else:
        print(f"Wrote {count} FAQ candidates to {output_path}")
    if publish_result is not None:
        state = "updated" if publish_result else "unchanged"
        print(f"Published FAQ knowledge doc {FAQ_KNOWLEDGE_FILENAME} ({state})")


if __name__ == "__main__":
    main()
