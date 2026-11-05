"""/devlog: staff read the private code notes (services/code_notes) of recent pushes to the mod repo."""

import logging

import discord
from discord.ext import commands

from bulmaai.services import code_notes
from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.ui.v2 import card, fit, trim
from bulmaai.utils.i18n import described, reader_language, t, table
from bulmaai.utils.permissions import in_main_guild, is_staff

log = logging.getLogger(__name__)

# Ephemeral, so in the reader's language; the notes themselves are English AI summaries.
TEXT = table({
    "en": {
        "staff_only": "Only staff can use this.",
        "failed": "Couldn't read the dev log right now.",
        "title": "Dev log",
        "none": "No code notes yet.",
        "no_match": "No code notes match that.",
        "footer": "-# Private AI summaries of pushes to the mod repo.",
    },
    "es": {
        "staff_only": "Solo el staff puede usar esto.",
        "failed": "No pude leer el registro de desarrollo ahora.",
        "title": "Registro de desarrollo",
        "none": "Todavía no hay notas de código.",
        "no_match": "Ninguna nota de código coincide con eso.",
        "footer": "-# Resúmenes privados hechos por IA de los pushes al repo del mod.",
    },
    "pt": {
        "staff_only": "Só a staff pode usar isso.",
        "failed": "Não consegui ler o registro de desenvolvimento agora.",
        "title": "Registro de desenvolvimento",
        "none": "Ainda não há notas de código.",
        "no_match": "Nenhuma nota de código corresponde a isso.",
        "footer": "-# Resumos privados feitos por IA dos pushes no repositório do mod.",
    },
})
DESC = table({
    "en": {
        "devlog": "Staff: recent notes on what changed in the mod's code",
        "devlog.query": "A system, config key, command or file to search for",
    },
    "es": {
        "devlog": "Staff: notas recientes de lo que cambió en el código del mod",
        "devlog.query": "Un sistema, clave de config, comando o archivo a buscar",
    },
    "pt": {
        "devlog": "Staff: notas recentes do que mudou no código do mod",
        "devlog.query": "Um sistema, chave de config, comando ou arquivo para buscar",
    },
})
LIST_LIMIT = 10
LINE_CHARS = 200


def note_line(note: code_notes.CodeNote) -> str:
    date = f"<t:{int(note.committed_at.timestamp())}:d>" if note.committed_at else "?"
    text = trim(defuse_mentions(note.one_line).replace("`", "'"), LINE_CHARS)
    return f"`{note.short_sha}` **{defuse_mentions(note.author)}** {date} · {text}"


def devlog_card(notes: list[code_notes.CodeNote], query: str = "", language: str = "en") -> discord.ui.DesignerView:
    name = t(TEXT, "title", language)
    title = f"## 🛠️ {name}: \"{trim(defuse_mentions(query), 60)}\"" if query else f"## 🛠️ {name}"
    if not notes:
        return card(title, t(TEXT, "no_match" if query else "none", language))
    lines = "\n".join(note_line(note) for note in notes)
    return card(*fit([title, lines, t(TEXT, "footer", language)]))


class DevlogCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="devlog", **described(DESC, "devlog"))
    @discord.option("query", str, **described(DESC, "devlog.query"), required=False)
    async def devlog(self, ctx: discord.ApplicationContext, query: str | None = None):
        settings = self.bot.settings
        member = ctx.author
        language = reader_language(ctx, settings)
        if not (isinstance(member, discord.Member) and in_main_guild(member, settings) and is_staff(member, settings=settings)):
            return await ctx.respond(t(TEXT, "staff_only", language), ephemeral=True)
        query = (query or "").strip()[:100]
        try:
            notes = await code_notes.search(query, LIST_LIMIT) if query else await code_notes.recent(LIST_LIMIT)
        except Exception:
            log.warning("/devlog failed", exc_info=True)
            return await ctx.respond(t(TEXT, "failed", language), ephemeral=True)
        await ctx.respond(view=devlog_card(notes, query, language), ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(DevlogCog(bot))
