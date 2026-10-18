"""One-shot export of the support knowledge that only exists in the old OpenAI vector stores.

Run it once on the VPS before the OpenAI key is retired:

    python scripts/export_openai_knowledge.py vs_abc vs_def

It writes every manually uploaded doc to data/knowledge/<name>.md, where the bot's knowledge sync picks it up
(source 'local'). Wiki pages and closed tickets are skipped: the bot rebuilds those from the wiki and the DB.
Then commit the new files and delete this script.
"""

import argparse
import os
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
API_BASE = "https://api.openai.com/v1"
TIMEOUT_SECONDS = 60
REBUILT_SOURCES = frozenset({"mediawiki", "github-wiki"})
REBUILT_PREFIXES = ("wiki--", "ticket-")


def is_rebuilt_elsewhere(filename: str, attributes: Any) -> bool:
    """True for wiki and ticket files: the bot rebuilds those, so exporting them would only make duplicates."""
    if isinstance(attributes, dict) and attributes.get("source") in REBUILT_SOURCES:
        return True
    return Path(filename or "").name.startswith(REBUILT_PREFIXES)


def export_filename(filename: str, file_id: str) -> str:
    stem = Path(filename or "").stem.strip() or file_id
    return f"{stem}.md"


def _get(session: requests.Session, path: str, **params: Any) -> dict[str, Any]:
    response = session.get(f"{API_BASE}{path}", params=params or None, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _vector_store_files(session: requests.Session, vector_store_id: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100}
        if after:
            params["after"] = after
        page = _get(session, f"/vector_stores/{vector_store_id}/files", **params)
        data = page.get("data") or []
        files.extend(data)
        if not page.get("has_more") or not data:
            return files
        after = page.get("last_id") or data[-1]["id"]


def _file_text(session: requests.Session, vector_store_id: str, file_id: str) -> str:
    content = _get(session, f"/vector_stores/{vector_store_id}/files/{file_id}/content")
    return "\n".join(str(part.get("text") or "") for part in content.get("data") or [] if part.get("type", "text") == "text")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export manually uploaded OpenAI knowledge files to data/knowledge.")
    parser.add_argument("vector_store_ids", nargs="+", help="Support vector store ids (vs_...).")
    parser.add_argument("--key-env", default="OPENAI_KEY", help="Env var that holds the OpenAI API key.")
    parser.add_argument("--force", action="store_true", help="Overwrite files that already exist in data/knowledge.")
    args = parser.parse_args()

    key = os.environ.get(args.key_env, "").strip()
    if not key:
        raise SystemExit(f"Set {args.key_env} to the OpenAI API key.")

    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    with requests.Session() as session:
        session.headers.update({"Authorization": f"Bearer {key}", "OpenAI-Beta": "assistants=v2"})
        for vector_store_id in args.vector_store_ids:
            for entry in _vector_store_files(session, vector_store_id):
                file_id = str(entry["id"])
                filename = str(_get(session, f"/files/{file_id}").get("filename") or "")
                if is_rebuilt_elsewhere(filename, entry.get("attributes")):
                    skipped += 1
                    continue
                target = KNOWLEDGE_DIR / export_filename(filename, file_id)
                if target.exists() and not args.force:
                    print(f"  exists  {target.name} (use --force to overwrite)")
                    continue
                text = _file_text(session, vector_store_id, file_id).strip()
                if not text:
                    print(f"  empty   {filename or file_id}")
                    continue
                target.write_text(text + "\n", encoding="utf-8", newline="\n")
                written += 1
                print(f"  wrote   {target.name}  <- {filename or file_id}")
    print(f"Done: wrote {written}, skipped {skipped} wiki/ticket files.")


if __name__ == "__main__":
    main()
