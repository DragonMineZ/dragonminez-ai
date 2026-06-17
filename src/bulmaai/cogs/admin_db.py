import logging

import discord
from discord.ext import commands

from bulmaai.database.db import get_pool
from bulmaai.utils.permissions import is_admin

log = logging.getLogger(__name__)

_RESULT_CHAR_LIMIT = 1800


def _format_rows(rows) -> str:
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    data = [[str(r[c]) if r[c] is not None else "NULL" for c in cols] for r in rows]
    widths = [max(len(c), *(len(d[i]) for d in data)) for i, c in enumerate(cols)]
    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    divider = "-+-".join("-" * w for w in widths)
    body = "\n".join(
        " | ".join(d[i].ljust(widths[i]) for i in range(len(cols))) for d in data
    )
    return f"{header}\n{divider}\n{body}"


async def _db_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    typed = (ctx.value or "").strip().lower()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        table_names = [row["table_name"] for row in rows]
    except Exception:
        table_names = []

    suggestions: list[str] = []
    for t in table_names:
        suggestions.append(f"SELECT * FROM {t} LIMIT 10")
    for t in table_names:
        suggestions.append(f"SELECT count(*) FROM {t}")

    if not typed:
        return suggestions[:25]
    return [s for s in suggestions if typed in s.lower()][:25]


class AdminDatabaseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.slash_command(
        name="database",
        description="Execute a PostgreSQL query (admin only)",
    )
    @discord.option(
        "query",
        description="SQL to run, e.g. SELECT * FROM patreon_links LIMIT 10",
        autocomplete=_db_autocomplete,
        required=True,
    )
    async def database(self, ctx: discord.ApplicationContext, query: str) -> None:
        if not isinstance(ctx.author, discord.Member) or not is_admin(ctx.author):
            await ctx.respond("Admins only.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        upper = query.strip().upper()
        is_read = upper.startswith(("SELECT", "EXPLAIN", "SHOW", "WITH", "TABLE", "VALUES"))

        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                if is_read:
                    rows = await conn.fetch(query)
                    count = len(rows)
                    table = _format_rows(rows)
                    status = f"{count} row(s)"
                else:
                    status_str = await conn.execute(query)
                    table = None
                    status = str(status_str)
        except Exception as exc:
            await ctx.followup.send(
                f"**Query error:**\n```\n{exc}\n```",
                ephemeral=True,
            )
            return

        if table is not None:
            raw = f"```\n{table}\n```"
            if len(raw) > _RESULT_CHAR_LIMIT:
                raw = f"```\n{table[:_RESULT_CHAR_LIMIT - 30]}\n...(truncated)\n```"
            await ctx.followup.send(f"**{status}:**\n{raw}", ephemeral=True)
        else:
            await ctx.followup.send(f"**Executed.** Status: `{status}`", ephemeral=True)


def setup(bot):
    bot.add_cog(AdminDatabaseCog(bot))
