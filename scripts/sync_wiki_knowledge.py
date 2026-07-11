import argparse
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bulmaai.config import load_settings
from bulmaai.services.wiki_knowledge import (
    apply_wiki_sync,
    list_remote_wiki_files,
    load_wiki_pages,
    plan_wiki_sync,
)


def _clone_wiki(git_url: str, destination: Path) -> Path:
    subprocess.run(
        ["git", "clone", "--depth", "1", git_url, str(destination)],
        check=True,
    )
    return destination


async def _run(
    *,
    wiki_dir: Path | None,
    wiki_git_url: str | None,
    vector_store_id: str | None,
    base_url: str | None,
    dry_run: bool,
) -> None:
    load_dotenv()
    settings = load_settings()

    target_vector_store_id = (
        vector_store_id
        or settings.openai_wiki_vector_store_id
        or next(iter(settings.openai_support_vector_store_ids), None)
    )
    if not target_vector_store_id:
        raise RuntimeError(
            "Set OPENAI_WIKI_VECTOR_STORE_ID (or OPENAI_SUPPORT_VECTOR_STORE_IDS) "
            "or pass --vector-store-id."
        )

    resolved_base_url = base_url or settings.wiki_base_url
    resolved_git_url = wiki_git_url or settings.wiki_git_url

    with tempfile.TemporaryDirectory(prefix="dmz-wiki-") as tmp:
        source_dir = wiki_dir or _clone_wiki(resolved_git_url, Path(tmp) / "wiki")
        pages = load_wiki_pages(source_dir, base_url=resolved_base_url)

    if not pages:
        raise RuntimeError(f"No wiki pages found in {source_dir}; refusing to sync an empty set.")

    remote_files = await list_remote_wiki_files(vector_store_id=target_vector_store_id)
    plan = plan_wiki_sync(pages, remote_files)

    print(
        f"Wiki pages: {len(pages)} | upload: {len(plan.uploads)} | "
        f"delete: {len(plan.deletes)} | unchanged: {plan.unchanged}"
    )
    for page in plan.uploads:
        print(f"  upload  {page.filename}")
    for remote in plan.deletes:
        print(f"  delete  {remote.wiki_slug} ({remote.file_id})")

    if dry_run:
        print("Dry run: no changes applied.")
        return

    summary = await apply_wiki_sync(plan, vector_store_id=target_vector_store_id)
    print(
        f"Sync complete: uploaded {summary['uploaded']}, deleted {summary['deleted']}, "
        f"unchanged {summary['unchanged']}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync the DragonMineZ GitHub wiki into the OpenAI support vector store."
    )
    parser.add_argument(
        "--wiki-dir",
        type=Path,
        default=None,
        help="Path to an already-cloned wiki checkout. Omit to clone --wiki-git-url.",
    )
    parser.add_argument(
        "--wiki-git-url",
        default=None,
        help="Wiki git URL to clone when --wiki-dir is not given. Defaults to WIKI_GIT_URL.",
    )
    parser.add_argument(
        "--vector-store-id",
        default=None,
        help="Target vector store. Defaults to OPENAI_WIKI_VECTOR_STORE_ID.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base wiki URL used for source links. Defaults to WIKI_BASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the sync plan without uploading or deleting anything.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(
            _run(
                wiki_dir=args.wiki_dir,
                wiki_git_url=args.wiki_git_url,
                vector_store_id=args.vector_store_id,
                base_url=args.base_url,
                dry_run=args.dry_run,
            )
        )
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
