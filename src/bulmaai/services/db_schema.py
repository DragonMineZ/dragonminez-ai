from pathlib import Path

from bulmaai.database.db import get_pool


async def ensure_schema() -> None:
    schema_path = Path(__file__).resolve().parents[3] / "scripts" / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql)
