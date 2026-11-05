"""/devlog: staff read the private code notes (services/code_notes) of recent pushes to the mod repo."""

import logging

import discord
from discord.ext import commands

from bulmaai.services import code_notes
from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.ui.v2 import card, fit, trim
from bulmaai.utils.permissions import in_main_guild, is_staff

log = logging.getLogger(__name__)

STAFF_ONLY_MSG = "Only staff can use this."
LIST_LIMIT = 10
LINE_CHARS = 200


def note_line(note: code_notes.CodeNote) -> str:
    date = f"<t:{int(note.committed_at.timestamp())}:d>" if note.committed_at else "?"
    text = trim(defuse_mentions(note.one_line).replace("`", "'"), LINE_CHARS)
    return f"`{note.short_sha}` **{defuse_mentions(note.author)}** {date} · {text}"


def devlog_card(notes: list[code_notes.CodeNote], query: str = "") -> discord.ui.DesignerView:
    title = f"## 🛠️ Dev log: \"{trim(defuse_mentions(query), 60)}\"" if query else "## 🛠️ Dev log"
    if not notes:
        return card(title, "No code notes yet." if not query else "No code notes match that.")
    return card(*fit([title, "\n".join(note_line(note) for note in notes), "-# Private AI summaries of pushes to the mod repo."]))


class DevlogCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="devlog", description="Staff: recent notes on what changed in the mod's code")
    @discord.option("query", str, description="A system, config key, command or file to search for", required=False)
    async def devlog(self, ctx: discord.ApplicationContext, query: str | None = None):
        settings = self.bot.settings
        member = ctx.author
        if not (isinstance(member, discord.Member) and in_main_guild(member, settings) and is_staff(member, settings=settings)):
            return await ctx.respond(STAFF_ONLY_MSG, ephemeral=True)
        query = (query or "").strip()[:100]
        try:
            notes = await code_notes.search(query, LIST_LIMIT) if query else await code_notes.recent(LIST_LIMIT)
        except Exception:
            log.warning("/devlog failed", exc_info=True)
            return await ctx.respond("Couldn't read the dev log right now.", ephemeral=True)
        await ctx.respond(view=devlog_card(notes, query), ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(DevlogCog(bot))
