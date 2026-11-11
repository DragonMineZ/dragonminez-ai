"""/tag: canned text answers by category (api, error, mixin, ...). Staff write them; anyone can show one."""

import logging

import discord
from discord.ext import commands

from bulmaai.services import tags as db
from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.ui.v2 import card, trim
from bulmaai.utils.i18n import described, reader_language, t, table
from bulmaai.utils.permissions import in_main_guild, is_staff

log = logging.getLogger(__name__)

TEXT = table({
    "en": {
        "staff_only": "Only staff can use this.",
        "not_found": "No tag `{category}/{name}`.",
        "saved": "Saved `{category}/{name}`.",
        "removed": "Removed `{category}/{name}`.",
        "none": "No tags yet.",
        "failed": "Couldn't read the tags right now.",
        "text": "Text",
        "modal": "Tag {category}/{name}",
        "list": "Tags",
    },
    "es": {
        "staff_only": "Solo el staff puede usar esto.",
        "not_found": "No existe la etiqueta `{category}/{name}`.",
        "saved": "Guardé `{category}/{name}`.",
        "removed": "Eliminé `{category}/{name}`.",
        "none": "Todavía no hay etiquetas.",
        "failed": "No pude leer las etiquetas ahora.",
        "text": "Texto",
        "modal": "Etiqueta {category}/{name}",
        "list": "Etiquetas",
    },
    "pt": {
        "staff_only": "Só a staff pode usar isso.",
        "not_found": "Não existe a tag `{category}/{name}`.",
        "saved": "Salvei `{category}/{name}`.",
        "removed": "Removi `{category}/{name}`.",
        "none": "Ainda não há tags.",
        "failed": "Não consegui ler as tags agora.",
        "text": "Texto",
        "modal": "Tag {category}/{name}",
        "list": "Tags",
    },
})
DESC = table({
    "en": {
        "tag": "Canned answers by category",
        "show": "Show a canned answer",
        "add": "Staff: add or edit a canned answer",
        "remove": "Staff: remove a canned answer",
        "list": "List the canned answers",
        "category": "Category",
        "name": "Tag name",
    },
    "es": {
        "tag": "Respuestas guardadas por categoría",
        "show": "Muestra una respuesta guardada",
        "add": "Staff: agrega o edita una respuesta guardada",
        "remove": "Staff: elimina una respuesta guardada",
        "list": "Lista las respuestas guardadas",
        "category": "Categoría",
        "name": "Nombre de la etiqueta",
    },
    "pt": {
        "tag": "Respostas salvas por categoria",
        "show": "Mostra uma resposta salva",
        "add": "Staff: adiciona ou edita uma resposta salva",
        "remove": "Staff: remove uma resposta salva",
        "list": "Lista as respostas salvas",
        "category": "Categoria",
        "name": "Nome da tag",
    },
})


def tag_card(tag: db.Tag) -> discord.ui.DesignerView:
    return card(f"## 🏷️ {tag.category}/{tag.name}", defuse_mentions(tag.body))


def list_text(pairs: list[tuple[str, str]], language: str = "en") -> str:
    if not pairs:
        return t(TEXT, "none", language)
    groups: dict[str, list[str]] = {}
    for category, name in pairs:
        groups.setdefault(category, []).append(f"`{name}`")
    lines = [f"**{category}**: {', '.join(found)}" for category, found in groups.items()]
    return trim(f"## 🏷️ {t(TEXT, 'list', language)}\n" + "\n".join(lines), 3900)


def _staff(user, settings) -> bool:
    return isinstance(user, discord.Member) and in_main_guild(user, settings) and is_staff(user, settings=settings)


async def _name_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    category = ctx.options.get("category")
    if category not in db.CATEGORIES:
        return []
    try:
        return [name for _, name in await db.names(category, ctx.value or "")]
    except Exception:
        log.info("Tag autocomplete failed", exc_info=True)
        return []


class TagModal(discord.ui.DesignerModal):
    def __init__(self, category: str, name: str, body: str, language: str) -> None:
        self.body_input = discord.ui.InputText(
            style=discord.InputTextStyle.paragraph, max_length=db.BODY_MAX, value=body or None
        )
        super().__init__(
            discord.ui.Label(t(TEXT, "text", language), self.body_input),
            title=trim(t(TEXT, "modal", language, category=category, name=name), 45),
        )
        self.category, self.name = category, name

    async def callback(self, interaction: discord.Interaction) -> None:
        language = reader_language(interaction)
        try:
            tag = await db.save(self.category, self.name, self.body_input.value or "", interaction.user.id)
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)
        await interaction.response.send_message(
            t(TEXT, "saved", language, category=tag.category, name=tag.name), ephemeral=True
        )


class TagsCog(commands.Cog):
    tag = discord.SlashCommandGroup("tag", **described(DESC, "tag"))

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @tag.command(name="show", **described(DESC, "show"))
    @discord.option("category", str, **described(DESC, "category"), choices=list(db.CATEGORIES))
    @discord.option("name", str, **described(DESC, "name"), autocomplete=_name_autocomplete)
    async def show(self, ctx: discord.ApplicationContext, category: str, name: str):
        language = reader_language(ctx)
        found = await db.get(category, name)
        if found is None:
            return await ctx.respond(t(TEXT, "not_found", language, category=category, name=name[:40]), ephemeral=True)
        # Staff post it for everyone; members see it privately so the command can't be used to spam.
        await ctx.respond(
            view=tag_card(found),
            ephemeral=not _staff(ctx.author, self.bot.settings),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tag.command(name="add", **described(DESC, "add"))
    @discord.option("category", str, **described(DESC, "category"), choices=list(db.CATEGORIES))
    @discord.option("name", str, **described(DESC, "name"), autocomplete=_name_autocomplete)
    async def add(self, ctx: discord.ApplicationContext, category: str, name: str):
        language = reader_language(ctx)
        if not _staff(ctx.author, self.bot.settings):
            return await ctx.respond(t(TEXT, "staff_only", language), ephemeral=True)
        try:
            name = db.clean_name(name)
        except ValueError as error:
            return await ctx.respond(str(error), ephemeral=True)
        existing = await db.get(category, name)
        await ctx.send_modal(TagModal(category, name, existing.body if existing else "", language))

    @tag.command(name="remove", **described(DESC, "remove"))
    @discord.option("category", str, **described(DESC, "category"), choices=list(db.CATEGORIES))
    @discord.option("name", str, **described(DESC, "name"), autocomplete=_name_autocomplete)
    async def remove(self, ctx: discord.ApplicationContext, category: str, name: str):
        language = reader_language(ctx)
        if not _staff(ctx.author, self.bot.settings):
            return await ctx.respond(t(TEXT, "staff_only", language), ephemeral=True)
        key = "removed" if await db.remove(category, name) else "not_found"
        await ctx.respond(t(TEXT, key, language, category=category, name=name[:40]), ephemeral=True)

    @tag.command(name="list", **described(DESC, "list"))
    @discord.option("category", str, **described(DESC, "category"), choices=list(db.CATEGORIES), required=False)
    async def list_tags(self, ctx: discord.ApplicationContext, category: str | None = None):
        language = reader_language(ctx)
        try:
            pairs = await db.names(category, limit=200)
        except Exception:
            log.warning("/tag list failed", exc_info=True)
            return await ctx.respond(t(TEXT, "failed", language), ephemeral=True)
        await ctx.respond(view=card(list_text(pairs, language)), ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(TagsCog(bot))
