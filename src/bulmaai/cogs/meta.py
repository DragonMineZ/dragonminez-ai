import logging
import time

import discord
from discord.ext import commands

from bulmaai.config import (
    format_setting_value,
    get_editable_setting_names,
    load_settings,
    load_settings_overrides,
    reset_setting_override,
    set_setting_override,
)
from bulmaai.ui.log_help_views import build_log_help_embeds
from bulmaai.utils.permissions import is_bruno


log = logging.getLogger(__name__)


def _setting_name_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    query = str(getattr(ctx, "value", "") or "").lower()
    return [
        name
        for name in get_editable_setting_names()
        if query in name.lower()
    ][:25]


def _chunk_lines(lines: list[str], *, limit: int = 1800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}".strip() if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class MetaCog(commands.Cog):
    """Meta (Utility) commands for the bot."""

    settings_group = discord.SlashCommandGroup("settings", "Manage runtime bot settings")

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    def _ensure_bruno(self, member: discord.abc.User) -> bool:
        return isinstance(member, discord.Member) and is_bruno(member)

    async def _reject_settings_access(self, ctx: discord.ApplicationContext) -> None:
        await ctx.respond("Only Bruno can change bot settings from Discord.", ephemeral=True)

    @discord.slash_command(name="ping", description="Check the bot's latency.")
    async def ping(self, ctx: discord.ApplicationContext):
        start_time = time.perf_counter()
        message = await ctx.respond("Pong!", wait=True)
        end_time = time.perf_counter()
        latency = (end_time - start_time) * 1000
        await message.edit(content=f"Pong! Latency: {latency:.2f} ms")

    @discord.slash_command(name="about", description="Get information about the bot.")
    async def about(self, ctx: discord.ApplicationContext):
        embed = discord.Embed(
            title="About BulmaAI",
            description="BulmaAI is a Discord bot, yay.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Version", value="1.0.0", inline=False)
        embed.add_field(name="Author", value="DragonMineZ Team", inline=False)
        await ctx.respond(embed=embed)

    @discord.slash_command(
        name="loghelp",
        description="Post info on finding latest.log or crash-report.txt",
    )
    @discord.option(
        "language",
        description="Language to post",
        choices=["English", "Espanol", "Portugues"],
        required=False,
    )
    async def loghelp(self, ctx: discord.ApplicationContext, language: str = "English"):
        lang_map = {"English": "en", "Espanol": "es", "Portugues": "pt"}
        lang_code = lang_map.get(language, "en")
        embeds = build_log_help_embeds(lang_code)
        await ctx.respond(embeds=embeds)

    @settings_group.command(name="get", description="Show a runtime setting")
    @discord.option(
        "name",
        description="Setting name",
        autocomplete=discord.utils.basic_autocomplete(_setting_name_autocomplete),
    )
    async def settings_get(self, ctx: discord.ApplicationContext, name: str):
        if not self._ensure_bruno(ctx.author):
            return await self._reject_settings_access(ctx)

        editable = set(get_editable_setting_names())
        if name not in editable:
            return await ctx.respond("Unknown setting name.", ephemeral=True)

        current = load_settings()
        defaults = load_settings(include_overrides=False)
        overrides = load_settings_overrides()

        embed = discord.Embed(
            title=f"Setting: {name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Current",
            value=f"```json\n{format_setting_value(getattr(current, name))}\n```",
            inline=False,
        )
        embed.add_field(
            name="Default",
            value=f"```json\n{format_setting_value(getattr(defaults, name))}\n```",
            inline=False,
        )
        embed.add_field(
            name="Override",
            value=(
                f"```json\n{format_setting_value(overrides[name])}\n```"
                if name in overrides
                else "`None`"
            ),
            inline=False,
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @settings_group.command(name="set", description="Persist a runtime setting override")
    @discord.option(
        "name",
        description="Setting name",
        autocomplete=discord.utils.basic_autocomplete(_setting_name_autocomplete),
    )
    @discord.option(
        "value",
        description="New value. Use commas or JSON arrays for lists; true/false for booleans.",
    )
    async def settings_set(self, ctx: discord.ApplicationContext, name: str, value: str):
        if not self._ensure_bruno(ctx.author):
            return await self._reject_settings_access(ctx)

        try:
            parsed_value = set_setting_override(name, value)
        except KeyError:
            return await ctx.respond("Unknown setting name.", ephemeral=True)
        except ValueError as error:
            return await ctx.respond(f"Invalid value for `{name}`: {error}", ephemeral=True)

        if hasattr(self.bot, "reload_settings"):
            self.bot.reload_settings()

        await ctx.respond(
            f"Saved override for `{name}` to `{format_setting_value(parsed_value)}`. "
            "Bot settings were reloaded; components that cache values may still need a restart.",
            ephemeral=True,
        )

    @settings_group.command(name="reset", description="Remove a runtime setting override")
    @discord.option(
        "name",
        description="Setting name",
        autocomplete=discord.utils.basic_autocomplete(_setting_name_autocomplete),
    )
    async def settings_reset(self, ctx: discord.ApplicationContext, name: str):
        if not self._ensure_bruno(ctx.author):
            return await self._reject_settings_access(ctx)

        try:
            reset_setting_override(name)
        except KeyError:
            return await ctx.respond("Unknown setting name.", ephemeral=True)

        if hasattr(self.bot, "reload_settings"):
            self.bot.reload_settings()

        current = load_settings()
        await ctx.respond(
            f"Reset override for `{name}`. Current value is now `{format_setting_value(getattr(current, name))}`.",
            ephemeral=True,
        )

    @settings_group.command(name="overrides", description="List saved runtime setting overrides")
    async def settings_overrides(self, ctx: discord.ApplicationContext):
        if not self._ensure_bruno(ctx.author):
            return await self._reject_settings_access(ctx)

        overrides = load_settings_overrides()
        if not overrides:
            return await ctx.respond("No runtime setting overrides are saved.", ephemeral=True)

        lines = [
            f"`{name}` = `{format_setting_value(value)}`"
            for name, value in sorted(overrides.items())
        ]
        chunks = _chunk_lines(lines)
        await ctx.respond(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await ctx.followup.send(chunk, ephemeral=True)

    @settings_group.command(name="keys", description="List editable setting names")
    async def settings_keys(self, ctx: discord.ApplicationContext):
        if not self._ensure_bruno(ctx.author):
            return await self._reject_settings_access(ctx)

        lines = [f"`{name}`" for name in get_editable_setting_names()]
        chunks = _chunk_lines(lines)
        await ctx.respond(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await ctx.followup.send(chunk, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(MetaCog(bot))
