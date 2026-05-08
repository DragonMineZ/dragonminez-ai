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
    publish_faq_markdown_to_vector_store,
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
    vector_store_id: str | None,
) -> tuple[int, Path, dict[str, str] | None]:
    load_dotenv()
    settings = load_settings()
    output_path = _repo_relative_path(output or settings.openai_faq_generated_path)
    suggestion_model = model or settings.openai_faq_suggestion_model
    target_vector_store_id = vector_store_id or settings.openai_faq_vector_store_id

    count = 0
    if not publish_existing:
        await init_db_pool()
        try:
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
        finally:
            await close_db_pool()

    publish_result = None
    if publish or publish_existing:
        if not target_vector_store_id:
            raise RuntimeError(
                "Set OPENAI_FAQ_VECTOR_STORE_ID or pass --vector-store-id before using --publish."
            )
        publish_result = await publish_faq_markdown_to_vector_store(
            output_path,
            vector_store_id=target_vector_store_id,
        )
    return len(candidates), output_path, publish_result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suggest DragonMineZ FAQ entries from support traces and write Markdown."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Markdown path to write. Defaults to OPENAI_FAQ_GENERATED_PATH.",
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
        help="OpenAI model for candidate generation. Defaults to OPENAI_FAQ_SUGGESTION_MODEL.",
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
        help="Generate Markdown, then upload it to the configured FAQ vector store.",
    )
    parser.add_argument(
        "--publish-existing",
        action="store_true",
        help="Upload the existing Markdown file without regenerating it.",
    )
    parser.add_argument(
        "--vector-store-id",
        default=None,
        help="Override OPENAI_FAQ_VECTOR_STORE_ID for --publish.",
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
                vector_store_id=args.vector_store_id,
            )
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.publish_existing:
        print(f"Publishing existing FAQ Markdown from {output_path}")
    else:
        print(f"Wrote {count} FAQ candidates to {output_path}")
    if publish_result is not None:
        print(
            "Uploaded FAQ file "
            f"{publish_result['file_id']} as vector store file "
            f"{publish_result['vector_store_file_id']}"
        )


if __name__ == "__main__":
    main()
