#!/usr/bin/env python3
import asyncio
import logging

from ingest_docs import main

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logging.getLogger(__name__).warning(
    "scripts/ingest_pdf.py is deprecated. Use scripts/ingest_docs.py instead."
)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
