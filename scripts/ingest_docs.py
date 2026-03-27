import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bulmaai.database.db import close_db_pool, init_db_pool
from bulmaai.services.docs_ingestion import (
    delete_source,
    ensure_schema,
    ingest_path,
    list_sources,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")


async def run_upload(args: argparse.Namespace) -> None:
    await init_db_pool()
    try:
        results = await ingest_path(
            path=args.path,
            lang=args.lang,
            source=args.source,
            replace=not args.append,
            recursive=not args.no_recursive,
            source_type=args.source_type,
            embedding_model=args.embedding_model,
        )
        for result in results:
            log.info(
                "Ingested %s (%s, %s): %d chunks across %d sections",
                result["source"],
                result["lang"],
                result["source_type"],
                result["chunks"],
                result["sections"],
            )
    finally:
        await close_db_pool()


async def run_list(_: argparse.Namespace) -> None:
    await init_db_pool()
    try:
        await ensure_schema()
        rows = await list_sources()
        if not rows:
            print("(no documents in the database)")
            return
        print(f"{'SOURCE':<40} {'LANG':<6} {'TYPE':<10} {'CHUNKS':>6} {'LAST UPDATE'}")
        print("-" * 96)
        for row in rows:
            print(
                f"{row['source']:<40} {row['lang']:<6} {row['source_type']:<10} "
                f"{row['chunks']:>6} {row['last_update']}"
            )
    finally:
        await close_db_pool()


async def run_delete(args: argparse.Namespace) -> None:
    await init_db_pool()
    try:
        await ensure_schema()
        deleted = await delete_source(args.source)
        if deleted:
            log.info("Deleted %d chunks for source '%s'.", deleted, args.source)
        else:
            log.info("No chunks found for source '%s'.", args.source)
    finally:
        await close_db_pool()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload and manage DragonMineZ support knowledge sources.",
    )
    sub = parser.add_subparsers(dest="command")

    upload = sub.add_parser("upload", help="Upload a file or directory into the docs database.")
    upload.add_argument("path", help="Path to a supported file or directory")
    upload.add_argument("--lang", default="en", choices=["en", "es", "pt"], help="Document language")
    upload.add_argument("--source", help="Custom source name. For directories, this becomes a source prefix.")
    upload.add_argument("--append", action="store_true", help="Append instead of replacing an existing source.")
    upload.add_argument("--no-recursive", action="store_true", help="Do not walk directories recursively.")
    upload.add_argument(
        "--source-type",
        default="auto",
        choices=["auto", "pdf", "text", "markdown", "json", "html"],
        help="Force the source parser type.",
    )
    upload.add_argument(
        "--embedding-model",
        help="Override the embedding model for this upload.",
    )

    sub.add_parser("list", help="List all known sources.")

    delete = sub.add_parser("delete", help="Delete all chunks for a source.")
    delete.add_argument("source", help="Source name to delete")

    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "upload":
        await run_upload(args)
        return 0
    if args.command == "list":
        await run_list(args)
        return 0
    if args.command == "delete":
        await run_delete(args)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
