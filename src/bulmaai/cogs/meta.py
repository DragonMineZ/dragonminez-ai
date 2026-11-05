import logging

import discord
from discord.ext import commands

from bulmaai.ui.log_help_views import build_log_help_card
from bulmaai.ui.v2 import card, fit, head, trim
from bulmaai.utils.i18n import DISCORD_LOCALES, described, reader_language, t, table
from bulmaai.utils.permissions import in_main_guild, is_bruno, is_staff
from bulmaai.web.core import Tier, tier_for


log = logging.getLogger(__name__)

BRUNO = Tier.OWNER + 1  # above every panel tier: /database and /log-corpus are his alone
# Who a command is for, where its in-code gate differs from what Discord shows: by default a command with
# default_member_permissions is staff's and one without is everyone's. Keys are qualified names; a group's key
# covers its subcommands, and the longest match wins.
ACCESS: dict[str, int] = {
    "ticket": Tier.HELPER,
    "ticket close": Tier.NONE,  # owners close their own tickets
    "ticket panel": Tier.MODERATOR,
    "github": Tier.HELPER,
    "github pr merge": Tier.ADMIN,
    "github pr close": Tier.ADMIN,
    "info": Tier.HELPER,
    "aisupport": Tier.HELPER,
    "support-report": Tier.HELPER,
    "patreon unlink": Tier.HELPER,
    **dict.fromkeys(("ban", "softban", "unban", "purge", "lock", "unlock", "slowmode", "lockdown", "raidmode"), Tier.MODERATOR),
    "database": BRUNO,
    "log-corpus": BRUNO,
}
SECTIONS = (
    (Tier.NONE, "🌐", "everyone"),
    (Tier.HELPER, "🛡️", "staff"),
    (Tier.MODERATOR, "🔨", "moderators"),
    (Tier.ADMIN, "⚙️", "admins"),
    (Tier.OWNER, "👑", "owners"),
    (BRUNO, "🔑", "bruno"),
)
# /help is ephemeral, so it is in the reader's language; /about and /loghelp post publicly (/loghelp has its own
# language option), so they stay as they are.
TEXT = table({
    "en": {
        "everyone": "Everyone",
        "staff": "Staff",
        "moderators": "Moderators",
        "admins": "Admins",
        "owners": "Owners",
        "bruno": "Bruno",
        "apps": "🖱️ **Apps** (right-click)",
        "intro": "## 📖 BulmaAI commands\n-# What you can use here. Type `/` and a name to run one.",
    },
    "es": {
        "everyone": "Todos",
        "staff": "Staff",
        "moderators": "Moderadores",
        "admins": "Admins",
        "owners": "Dueños",
        "bruno": "Bruno",
        "apps": "🖱️ **Apps** (clic derecho)",
        "intro": "## 📖 Comandos de BulmaAI\n-# Lo que puedes usar aquí. Escribe `/` y un nombre para usar uno.",
    },
    "pt": {
        "everyone": "Todos",
        "staff": "Staff",
        "moderators": "Moderadores",
        "admins": "Admins",
        "owners": "Donos",
        "bruno": "Bruno",
        "apps": "🖱️ **Apps** (clique direito)",
        "intro": "## 📖 Comandos da BulmaAI\n-# O que você pode usar aqui. Digite `/` e um nome para usar um.",
    },
})
DESC = table({
    "en": {
        "about": "Get information about the bot.",
        "help": "The commands you can use here",
        "loghelp": "Post info on finding latest.log or crash-report.txt",
        "loghelp.language": "Language to post",
    },
    "es": {
        "about": "Información sobre el bot.",
        "help": "Los comandos que puedes usar aquí",
        "loghelp": "Publica cómo encontrar latest.log o crash-report.txt",
        "loghelp.language": "Idioma de la publicación",
    },
    "pt": {
        "about": "Informações sobre o bot.",
        "help": "Os comandos que você pode usar aqui",
        "loghelp": "Publica como encontrar o latest.log ou o crash-report.txt",
        "loghelp.language": "Idioma da publicação",
    },
})


def required_level(command) -> int:
    name = command.qualified_name
    while True:
        if name in ACCESS:
            return ACCESS[name]
        if " " not in name:
            break
        name = name.rsplit(" ", 1)[0]
    node = command
    while node is not None:
        if node.default_member_permissions is not None:
            return Tier.HELPER
        node = node.parent
    return Tier.NONE


def viewer_level(user, settings) -> int:
    """Outside the main server (DMs, other servers) everyone is a member: staff tools only work there."""
    if not isinstance(user, discord.Member) or not in_main_guild(user, settings):
        return Tier.NONE
    if is_bruno(user):
        return BRUNO
    tier = tier_for(user, settings)
    return max(tier, Tier.HELPER) if is_staff(user, settings=settings) else tier


def _leaves(commands_):
    for command in commands_:
        if isinstance(command, discord.SlashCommandGroup):
            yield from _leaves(command.subcommands)
        else:
            yield command


def _root(command):
    while command.parent is not None:
        command = command.parent
    return command


def _description(command, language: str) -> str:
    """The command's description in language, from its description_localizations (English without one)."""
    localized = getattr(command, "description_localizations", None)
    if isinstance(localized, dict):
        for locale in DISCORD_LOCALES.get(language, ()):
            if localized.get(locale):
                return localized[locale]
    return command.description


def help_sections(commands_, level: int, guild_id: int | None, language: str = "en") -> list[str]:
    """One text block per access level the viewer has, listing what they can run here, in language."""
    grouped: dict[int, dict[str, list]] = {}
    apps: dict[int, list[str]] = {}
    for command in _leaves(commands_):
        root = _root(command)
        if root.guild_ids and guild_id not in root.guild_ids:
            continue
        if guild_id is None and root.contexts is not None and discord.InteractionContextType.bot_dm not in root.contexts:
            continue
        needed = required_level(command)
        if needed > level:
            continue
        if not isinstance(command, discord.SlashCommand):
            apps.setdefault(needed, []).append(f"`{command.name}`")
            continue
        grouped.setdefault(needed, {}).setdefault(root.name, []).append(command)
    sections = []
    for needed, emoji, key in SECTIONS:
        title = f"{emoji} {t(TEXT, key, language)}"
        lines = []
        for name, commands_here in grouped.get(needed, {}).items():
            if len(commands_here) == 1 and commands_here[0].parent is None:
                description = trim(_description(commands_here[0], language), 80).rstrip(".")
                for mention in ("@everyone", "@here"):
                    description = description.replace(mention, f"`{mention}`")
                lines.append(f"`/{name}` {description}")
            else:
                subs = " · ".join(command.qualified_name.split(" ", 1)[1] for command in commands_here)
                lines.append(f"`/{name}` {subs}")
        if needed in apps:
            lines.append(f"{t(TEXT, 'apps', language)} " + ", ".join(apps[needed]))
        if lines:
            sections.append(f"### {title}\n" + "\n".join(lines))
    return sections


class MetaCog(commands.Cog):
    """Meta (Utility) commands for the bot."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="about", **described(DESC, "about"))
    async def about(self, ctx: discord.ApplicationContext):
        about = (
            "## About BulmaAI\n"
            "BulmaAI helps the DragonMineZ server with support, announcements, logs, and staff tooling.\n"
            "**Version** 1.8.4　**Author** DragonMineZ Team"
        )
        await ctx.respond(view=card(about, color=discord.Color.blue()))

    @discord.slash_command(name="help", **described(DESC, "help"))
    async def help(self, ctx: discord.ApplicationContext):
        level = viewer_level(ctx.author, self.bot.settings)
        language = reader_language(ctx, self.bot.settings)
        sections = help_sections(self.bot.pending_application_commands, level, ctx.guild_id, language)
        intro = t(TEXT, "intro", language)
        intro, *blocks = fit([intro, *sections])
        avatar = self.bot.user.display_avatar.url if self.bot.user else None
        await ctx.respond(view=card(head(intro, avatar), *blocks, color=discord.Color.blue()), ephemeral=True)

    @discord.slash_command(name="loghelp", **described(DESC, "loghelp"))
    @discord.option(
        "language",
        **described(DESC, "loghelp.language"),
        choices=["English", "Espanol", "Portugues"],
        required=False,
    )
    async def loghelp(self, ctx: discord.ApplicationContext, language: str = "English"):
        lang_map = {"English": "en", "Espanol": "es", "Portugues": "pt"}
        lang_code = lang_map.get(language, "en")
        await ctx.respond(view=build_log_help_card(lang_code))


def setup(bot: discord.Bot):
    bot.add_cog(MetaCog(bot))
