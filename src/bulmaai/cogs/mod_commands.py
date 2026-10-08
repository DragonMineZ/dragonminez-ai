"""Dyno-style moderation slash commands and context menus. Every action goes through mod_actions.perform();
access follows the panel's staff tiers (web/core.py PERMISSIONS), default_permissions only hides the commands."""

import asyncio
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from bulmaai.services import mod_actions, mod_cases, panel_logs, scam_images
from bulmaai.services.mod_actions import (
    MAX_TIMEOUT_SECONDS,
    ActionResult,
    LadderStep,
    ModActionError,
    format_duration,
    parse_duration_seconds,
    parse_ladder,
    pick_step,
)
from bulmaai.ui.mod_cards import reply_card
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.web.core import PERMISSIONS, resolve_member, tier_for

log = logging.getLogger(__name__)

MAX_REASON_LENGTH = 400  # the panel's cap; leaves room for the audit-log suffix under Discord's 512
BAN_DELETE_SECONDS = {"none": 0, "1h": 3600, "24h": 86400, "7d": 7 * 86400}
STAFF_ONLY = discord.Permissions(moderate_members=True)
# Dyno-style confirmation: emoji, "<name> <phrase>", accent colour.
VERBS = {
    "warn": ("⚠️", "has been warned", discord.Color.gold()),
    "note": ("📝", "has a new staff note", discord.Color.blurple()),
    "timeout": ("🔇", "has been timed out", discord.Color.orange()),
    "untimeout": ("🔊", "is no longer timed out", discord.Color.green()),
    "kick": ("👢", "has been kicked", discord.Color.dark_orange()),
    "ban": ("🔨", "has been banned", discord.Color.red()),
    "softban": ("🧹", "has been softbanned", discord.Color.dark_orange()),
    "unban": ("🕊️", "has been unbanned", discord.Color.green()),
}
WARNINGS_PER_PAGE = 5
NO_PINGS = discord.AllowedMentions.none()

_LINK_RE = re.compile(r"https?://", re.IGNORECASE)
_INVITE_RE = re.compile(r"discord(?:\.gg|(?:app)?\.com/invite)/", re.IGNORECASE)
PURGE_KINDS: dict[str, Callable[[discord.Message], bool]] = {
    "bots": lambda m: m.author.bot,
    "humans": lambda m: not m.author.bot,
    "links": lambda m: bool(_LINK_RE.search(m.content)),
    "images": lambda m: any((a.content_type or "").startswith("image/") for a in m.attachments),
    "attachments": lambda m: bool(m.attachments),
    "embeds": lambda m: bool(m.embeds),
    "invites": lambda m: bool(_INVITE_RE.search(m.content)),
}


def purge_check(
    *, user_id: int | None = None, contains: str | None = None, kind: str | None = None
) -> Callable[[discord.Message], bool]:
    """All filters ANDed; pinned messages are always kept."""
    needle = (contains or "").lower()
    matches_kind = PURGE_KINDS[kind] if kind else lambda _message: True

    def check(message: discord.Message) -> bool:
        return (
            not message.pinned
            and (user_id is None or message.author.id == user_id)
            and needle in message.content.lower()
            and matches_kind(message)
        )

    return check


def _step_text(step: LadderStep) -> str:
    return f"timeout {format_duration(step.duration_seconds)}" if step.action == "timeout" else step.action


def _ladder_bar(count: int, needed: int) -> str:
    filled = min(count, needed, 10)
    return "▰" * filled + "▱" * (min(needed, 10) - filled)


def _case_lines(cases: list[mod_cases.ModCase]) -> str:
    lines = []
    for case in cases:
        reason = (case.reason or "no reason").replace("\n", " ")
        reason = discord.utils.escape_markdown(reason if len(reason) <= 60 else reason[:59] + "…")
        line = f"`#{case.id}` **{case.action}** · {case.source} · {discord.utils.format_dt(case.created_at, 'R')} · {reason}"
        lines.append(line if case.active else f"~~{line}~~")
    return "\n".join(lines)[:4096]


class ReasonModal(discord.ui.Modal):
    def __init__(self, title: str, submit: Callable[[discord.Interaction, str], Awaitable[object]]):
        super().__init__(title=title[:45])
        self.reason_input = discord.ui.InputText(label="Reason", style=discord.InputTextStyle.long, max_length=300)
        self.add_item(self.reason_input)
        self._submit = submit

    async def callback(self, interaction: discord.Interaction):
        await self._submit(interaction, self.reason_input.value.strip())


class ConfirmView(discord.ui.DesignerView):
    """Private "are you sure?" card. On confirm it turns into "Working…" and on_confirm runs;
    on_confirm posts its own (public) result with interaction.followup.send."""

    def __init__(self, prompt: str, label: str, on_confirm: Callable[[discord.Interaction], Awaitable[object]]):
        confirm = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm.callback, cancel.callback = self._confirm, self._cancel
        super().__init__(
            discord.ui.Container(
                discord.ui.TextDisplay(prompt), discord.ui.ActionRow(confirm, cancel), color=discord.Color.gold()
            ),
            timeout=120,
            disable_on_timeout=True,
        )
        self._on_confirm = on_confirm

    async def _confirm(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=reply_card("⏳ Working…", discord.Color.dark_grey()))
        await self._on_confirm(interaction)
        await interaction.delete_original_response()

    async def _cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=reply_card("Cancelled.", discord.Color.dark_grey()))


class ModCommandsCog(ReloadableCog):
    lockdown_group = discord.SlashCommandGroup(
        "lockdown", "Lock or unlock every public channel", default_member_permissions=STAFF_ONLY
    )

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    async def _allowed(self, ctx: discord.ApplicationContext | discord.Interaction, capability: str) -> bool:
        if tier_for(ctx.user, self.bot.settings) >= PERMISSIONS[capability]:
            return True
        await ctx.respond("Your staff tier can't do that.", ephemeral=True)
        return False

    @staticmethod
    async def _private_error(ctx: discord.ApplicationContext, text: str) -> None:
        """An error after a public defer: drop the public "thinking…" so only the moderator sees it."""
        interaction = getattr(ctx, "interaction", ctx)
        if interaction.response.is_done():
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
            await ctx.followup.send(text, ephemeral=True)
        else:
            await ctx.respond(text, ephemeral=True)

    async def _post_mod_log(self, kind: str, title: str, text: str, *, user_id: int, data: dict | None = None) -> None:
        """Best effort: the panel log plus a grey card in the moderation log channel."""
        try:
            await panel_logs.record(kind, title, text, user_id=user_id, data=data)
            channel = await mod_actions.resolve_channel(self.bot, mod_actions.mod_log_channel_id(self.bot.settings))
            if channel is not None:
                card = reply_card(f"### {title}\n{text}"[:3800], discord.Color.dark_grey())
                await channel.send(view=card, allowed_mentions=NO_PINGS)
        except Exception:
            log.warning("Failed to post %s to the moderation log", kind, exc_info=True)

    async def _display_name(self, user_id: int) -> str:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return f"<@{user_id}>"
        return discord.utils.escape_markdown(user.name)

    async def _perform(self, ctx, action: str, target_id: int, reason: str, **options) -> ActionResult | None:
        """ctx is an ApplicationContext or a modal's Interaction; replies to it either way.
        Confirmations are public like Dyno's (the reason isn't shown); errors stay private."""
        interaction = getattr(ctx, "interaction", ctx)
        # DM + Discord call + DB + mod-log post (+ a ladder step) can outlast the 3s interaction window.
        deferred_here = not interaction.response.is_done()
        if deferred_here:
            await interaction.response.defer()
        try:
            result = await mod_actions.perform(
                self.bot, ctx.guild, action=action, target_id=target_id, moderator=ctx.user, reason=reason, **options
            )
        except ModActionError as error:
            if deferred_here:
                await interaction.delete_original_response()  # drop the public "thinking…" so the error stays private
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return None
        emoji, phrase, color = VERBS[action]
        text = f"{emoji} ***{await self._display_name(target_id)} {phrase}"
        if options.get("duration_seconds"):
            text += f" for {format_duration(options['duration_seconds'])}"
        text += ".***"
        if escalation := result.escalation:
            duration = escalation.duration_seconds
            text += f"\n⚡ Warn ladder kicked in: **{escalation.action}"
            text += (f" {format_duration(duration)}" if duration else "") + "**"
            text += f" (case #{escalation.case_id})" if escalation.case_id else ""
        if result.ladder_skipped:
            text += f"\n⚠️ Warn ladder: {result.ladder_skipped}"
        footer = f"Case #{result.case_id}" if result.case_id else "The case couldn't be recorded"
        await ctx.respond(view=reply_card(text, color, footer), allowed_mentions=NO_PINGS)
        return result

    # --- warnings and notes ------------------------------------------------------------------

    @discord.slash_command(name="warn", description="Warn a member (counts toward the warn ladder)")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to warn")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    async def warn(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.warn"):
            await self._perform(ctx, "warn", user.id, reason)

    @discord.slash_command(name="warnings", description="A member's active warnings and warn-ladder standing")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to look up")
    async def warnings(self, ctx: discord.ApplicationContext, user: discord.User):
        if await self._allowed(ctx, "mod.cases.view"):
            await ctx.respond(view=await self._warnings_view(ctx.guild.id, user), allowed_mentions=NO_PINGS)

    async def _ladder_text(self, guild_id: int, user_id: int) -> str | None:
        steps = parse_ladder(self.bot.settings.moderation_warn_ladder)
        if not steps:
            return None
        now = discord.utils.utcnow()
        counts = {
            window: await mod_cases.count_active_since(
                guild_id, user_id, "warn", now - timedelta(seconds=window) if window else None
            )
            for window in {step.window_seconds for step in steps}
        }
        lines = [
            f"`{_ladder_bar(counts[step.window_seconds], step.warns)}` **{counts[step.window_seconds]}/{step.warns}** in "
            f"{format_duration(step.window_seconds) if step.window_seconds else 'all time'} → {_step_text(step)}"
            for step in steps
        ]
        upcoming = pick_step(steps, {window: count + 1 for window, count in counts.items()})
        lines.append(f"-# Next warn → {f'**{_step_text(upcoming)}**' if upcoming else 'no automatic action'}")
        return "### Warn ladder\n" + "\n".join(lines)

    async def _warnings_view(self, guild_id: int, user: discord.abc.User, page: int = 0) -> discord.ui.DesignerView:
        """Components V2 card in Dyno's layout: one row per warning with a delete button, then the ladder."""
        warns = [c for c in await mod_cases.list_cases(guild_id, user_id=user.id, action="warn", limit=100) if c.active]
        pages = max(1, -(-len(warns) // WARNINGS_PER_PAGE))
        page = max(0, min(page, pages - 1))
        items: list[discord.ui.Item] = [
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"## Warnings for {discord.utils.escape_markdown(user.name)}\n-# {user.mention} · `{user.id}`"
                ),
                accessory=discord.ui.Thumbnail(user.display_avatar.url),
            ),
            discord.ui.Separator(),
        ]
        for case in warns[page * WARNINGS_PER_PAGE : (page + 1) * WARNINGS_PER_PAGE]:
            reason = (case.reason or "No reason").replace("\n", " ")
            reason = discord.utils.escape_markdown(reason if len(reason) <= 200 else reason[:199] + "…")
            moderator = f"<@{case.moderator_id}>" if case.moderator_id else case.source
            delete = discord.ui.Button(emoji="🗑️", style=discord.ButtonStyle.danger)
            delete.callback = self._warn_delete_callback(guild_id, user, case.id, page)
            when = discord.utils.format_dt(case.created_at, "R")
            items.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(f"**{reason}**\n-# Mod: {moderator} · {when} · Case #{case.id}"),
                    accessory=delete,
                )
            )
        if not warns:
            items.append(discord.ui.TextDisplay("✨ No active warnings, clean record."))
        if ladder := await self._ladder_text(guild_id, user.id):
            items += [discord.ui.Separator(), discord.ui.TextDisplay(ladder)]
        items += [
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"-# Page {page + 1}/{pages} ({len(warns)} warning{'' if len(warns) == 1 else 's'})"),
        ]
        view_items: list[discord.ui.Item] = [
            discord.ui.Container(*items, color=discord.Color.red() if warns else discord.Color.green())
        ]
        if pages > 1:
            buttons = []
            for label, target, disabled in (("◀", page - 1, page == 0), ("▶", page + 1, page >= pages - 1)):
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)
                button.callback = self._warn_page_callback(guild_id, user, target)
                buttons.append(button)
            view_items.append(discord.ui.ActionRow(*buttons))
        return discord.ui.DesignerView(*view_items, timeout=900, disable_on_timeout=True)

    def _warn_page_callback(self, guild_id: int, user: discord.abc.User, page: int):
        async def callback(interaction: discord.Interaction):
            if await self._allowed(interaction, "mod.cases.view"):
                view = await self._warnings_view(guild_id, user, page)
                await interaction.response.edit_message(view=view, allowed_mentions=NO_PINGS)

        return callback

    def _warn_delete_callback(self, guild_id: int, user: discord.abc.User, case_id: int, page: int):
        async def callback(interaction: discord.Interaction):
            if not (await self._allowed(interaction, "mod.cases.remove") and await self._outranks(interaction, user.id)):
                return
            removed = await mod_actions.end_case(self.bot, guild_id, case_id, ended_by=interaction.user.id)
            view = await self._warnings_view(guild_id, user, page)
            await interaction.response.edit_message(view=view, allowed_mentions=NO_PINGS)
            if removed is None:
                await interaction.followup.send(f"Case #{case_id} was already removed.", ephemeral=True)
            else:
                text = f"Removed warning #{case_id} for <@{user.id}>."
                await self._log_removal("Warning removed", text, interaction.user.id, user.id)

        return callback

    async def _outranks(self, ctx: discord.ApplicationContext | discord.Interaction, user_id: int) -> bool:
        """Removing cases follows the same rules as acting on the user (helpers can't clear a mod's warns)."""
        target = await resolve_member(ctx.guild, user_id)
        try:
            mod_actions.check_hierarchy(self.bot, ctx.guild, ctx.user, user_id, target, discord_action=False)
        except ModActionError as error:
            await ctx.respond(str(error), ephemeral=True)
            return False
        return True

    async def _remove_case(self, ctx: discord.ApplicationContext, case_id: int, action: str) -> None:
        if not await self._allowed(ctx, "mod.cases.remove"):
            return
        label = "warning" if action == "warn" else action
        case = await mod_cases.get_case(ctx.guild.id, case_id)
        if case is None or case.action != action:
            return await ctx.respond(f"Case #{case_id} isn't a {label}.", ephemeral=True)
        if not await self._outranks(ctx, case.user_id):
            return
        if await mod_actions.end_case(self.bot, ctx.guild.id, case_id, ended_by=ctx.user.id) is None:
            return await ctx.respond(f"Case #{case_id} was already removed.", ephemeral=True)
        text = f"Removed {label} #{case_id} for <@{case.user_id}>."
        await ctx.respond(text, allowed_mentions=NO_PINGS)
        await self._log_removal(f"{label.title()} removed", text, ctx.user.id, case.user_id)

    async def _log_removal(self, title: str, text: str, moderator_id: int, user_id: int) -> None:
        await self._post_mod_log(
            "case_removed", title, f"{text}\nBy <@{moderator_id}>", user_id=moderator_id, data={"target_id": user_id}
        )

    @discord.slash_command(name="delwarn", description="Remove a warning (it stops counting toward the ladder)")
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, description="Warning case number", min_value=1)
    async def delwarn(self, ctx: discord.ApplicationContext, case_id: int):
        await self._remove_case(ctx, case_id, "warn")

    @discord.slash_command(name="delnote", description="Remove a staff note")
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, description="Note case number", min_value=1)
    async def delnote(self, ctx: discord.ApplicationContext, case_id: int):
        await self._remove_case(ctx, case_id, "note")

    @discord.slash_command(name="clearwarns", description="Remove all of a member's active warnings")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member whose warnings to clear")
    async def clearwarns(self, ctx: discord.ApplicationContext, user: discord.User):
        if not (await self._allowed(ctx, "mod.cases.remove") and await self._outranks(ctx, user.id)):
            return

        async def clear(interaction: discord.Interaction):
            count = await mod_actions.end_user_cases(self.bot, ctx.guild.id, user.id, "warn", ended_by=ctx.user.id)
            text = f"Cleared {count} warning(s) for <@{user.id}>."
            await interaction.followup.send(text, allowed_mentions=NO_PINGS)
            await self._log_removal("Warnings cleared", text, ctx.user.id, user.id)

        await ctx.respond(
            view=ConfirmView(f"**Clear all active warnings for <@{user.id}>?**", "Clear warnings", clear),
            ephemeral=True,
            allowed_mentions=NO_PINGS,
        )

    @discord.slash_command(name="note", description="Add a staff-only note to a user")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to note")
    @discord.option("text", str, description="The note", max_length=MAX_REASON_LENGTH)
    async def note(self, ctx: discord.ApplicationContext, user: discord.User, text: str):
        if await self._allowed(ctx, "mod.warn"):
            await self._perform(ctx, "note", user.id, text)

    @discord.slash_command(name="notes", description="A user's staff notes")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to look up")
    async def notes(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        cases = await mod_cases.list_cases(ctx.guild.id, user_id=user.id, action="note", limit=100)
        notes = [case for case in cases if case.active]
        text = f"### 📝 Notes for {discord.utils.escape_markdown(str(user))}\n{_case_lines(notes[:25]) or 'No notes.'}"
        await ctx.respond(view=reply_card(text[:3800], discord.Color.blurple()), allowed_mentions=NO_PINGS)

    # --- actions -----------------------------------------------------------------------------

    @discord.slash_command(name="mute", description="Time a member out")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to time out")
    @discord.option("duration", str, description="e.g. 30m, 2h, 1d (max 28d)")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    async def mute(self, ctx: discord.ApplicationContext, user: discord.User, duration: str, reason: str):
        if not await self._allowed(ctx, "mod.timeout"):
            return
        seconds = parse_duration_seconds(duration)
        if not seconds:
            return await ctx.respond("Couldn't read that duration, try 30m, 2h or 1d.", ephemeral=True)
        await self._perform(ctx, "timeout", user.id, reason, duration_seconds=min(seconds, MAX_TIMEOUT_SECONDS))

    @discord.slash_command(name="unmute", description="Remove a member's timeout")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to un-time-out")
    @discord.option("reason", str, description="Optional", max_length=MAX_REASON_LENGTH, required=False)
    async def unmute(self, ctx: discord.ApplicationContext, user: discord.User, reason: str = ""):
        if await self._allowed(ctx, "mod.timeout"):
            await self._perform(ctx, "untimeout", user.id, reason or "")

    @discord.slash_command(name="kick", description="Kick a member")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="Member to kick")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    async def kick(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.kick"):
            await self._perform(ctx, "kick", user.id, reason)

    @discord.slash_command(name="ban", description="Ban a user, optionally for a limited time")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to ban (IDs of non-members work)")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    @discord.option("duration", str, description="e.g. 12h, 7d, 2w; empty = permanent", required=False)
    @discord.option("delete_messages", str, description="Delete their recent messages", choices=list(BAN_DELETE_SECONDS))
    async def ban(
        self,
        ctx: discord.ApplicationContext,
        user: discord.User,
        reason: str,
        duration: str = None,
        delete_messages: str = "none",
    ):
        if not await self._allowed(ctx, "mod.ban"):
            return
        seconds = parse_duration_seconds(duration) if duration else None
        if duration and not seconds:
            return await ctx.respond(
                "Couldn't read that duration, try 12h, 7d or 2w (leave it empty for a permanent ban).", ephemeral=True
            )
        await self._perform(
            ctx,
            "ban",
            user.id,
            reason,
            duration_seconds=seconds,
            delete_message_seconds=BAN_DELETE_SECONDS[delete_messages],
        )

    @discord.slash_command(name="softban", description="Ban and unban to delete a day of messages")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to softban")
    @discord.option("reason", str, description="Shown to them in the DM", max_length=MAX_REASON_LENGTH)
    async def softban(self, ctx: discord.ApplicationContext, user: discord.User, reason: str):
        if await self._allowed(ctx, "mod.ban"):
            await self._perform(ctx, "softban", user.id, reason)

    @discord.slash_command(name="unban", description="Unban a user by ID")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user_id", str, description="Their Discord user ID")
    @discord.option("reason", str, description="Why", max_length=MAX_REASON_LENGTH)
    async def unban(self, ctx: discord.ApplicationContext, user_id: str, reason: str):
        if not await self._allowed(ctx, "mod.ban"):
            return
        user_id = user_id.strip().strip("<@!>")
        if not re.fullmatch(r"[0-9]{15,20}", user_id):
            return await ctx.respond("That isn't a valid user ID.", ephemeral=True)
        await self._perform(ctx, "unban", int(user_id), reason)

    @discord.slash_command(name="purge", description="Bulk-delete recent messages in this channel")
    @discord.default_permissions(moderate_members=True)
    @discord.option("amount", int, description="How many recent messages to scan", min_value=1, max_value=500)
    @discord.option("user", discord.User, description="Only this user's messages", required=False)
    @discord.option("contains", str, description="Only messages containing this text", required=False)
    @discord.option("kind", str, description="Only this kind of message", choices=list(PURGE_KINDS), required=False)
    async def purge(
        self,
        ctx: discord.ApplicationContext,
        amount: int,
        user: discord.User = None,
        contains: str = None,
        kind: str = None,
    ):
        if not await self._allowed(ctx, "mod.channels"):
            return
        filters = [f"from <@{user.id}>" if user else "", f'containing "{contains}"' if contains else "", kind or ""]
        filter_text = ", ".join(f for f in filters if f)

        async def run(interaction: discord.Interaction):
            try:
                deleted = await ctx.channel.purge(
                    limit=amount,
                    check=purge_check(user_id=user.id if user else None, contains=contains, kind=kind),
                    after=discord.utils.utcnow() - timedelta(days=14),  # older ones can't be bulk-deleted
                    reason=f"/purge by {ctx.user.name}",
                )
            except discord.HTTPException:
                return await interaction.followup.send("Discord refused: the bot can't delete messages here.", ephemeral=True)
            await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s) from the last 14 days.")
            if not deleted:
                return
            text = f"**Channel** {ctx.channel.mention}　**Moderator** {ctx.user.mention}　**Deleted** {len(deleted)}"
            if filter_text:
                text += f"\n**Filters** {filter_text[:1000]}"
            await self._post_mod_log("purge", "🧹 Purge", text, user_id=ctx.user.id, data={"channel_id": ctx.channel.id})

        prompt = f"Scan the last **{amount}** message(s) here and delete " + (
            f"the ones {filter_text}?" if filter_text else "all of them?"
        )
        await ctx.respond(view=ConfirmView(prompt, "Purge", run), ephemeral=True, allowed_mentions=NO_PINGS)

    # --- cases -------------------------------------------------------------------------------

    @discord.slash_command(name="case", description="Show a moderation case")
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, description="Case number", min_value=1)
    async def case_show(self, ctx: discord.ApplicationContext, case_id: int):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        case = await mod_cases.get_case(ctx.guild.id, case_id)
        if case is None:
            return await ctx.respond("Unknown case.", ephemeral=True)
        await ctx.respond(view=await mod_actions.render_case_card(self.bot, case), allowed_mentions=NO_PINGS)

    @discord.slash_command(
        name="reason", description="Fix the reason on an existing case (find case numbers with /modlogs or /warnings)"
    )
    @discord.default_permissions(moderate_members=True)
    @discord.option("case_id", int, description="The case number, e.g. 42 for case #42", min_value=1)
    @discord.option(
        "reason", str, description="Replaces the old reason; the user isn't DMed again", max_length=MAX_REASON_LENGTH
    )
    async def case_reason(self, ctx: discord.ApplicationContext, case_id: int, reason: str):
        if not await self._allowed(ctx, "mod.cases.edit"):
            return
        case = await mod_cases.get_case(ctx.guild.id, case_id)
        if case is None:
            return await ctx.respond(f"There's no case #{case_id}. Check /modlogs for the user's case numbers.", ephemeral=True)
        if not await self._outranks(ctx, case.user_id):
            return
        await mod_cases.update_reason(ctx.guild.id, case_id, reason.strip())
        if updated := await mod_cases.get_case(ctx.guild.id, case_id):
            await mod_actions.refresh_case_card(self.bot, updated)  # the mod-log card shows the new reason too
        await ctx.respond(
            f"Case #{case_id} ({case.action} on <@{case.user_id}>) reason changed:\n"
            f"~~{case.reason or 'No reason'}~~ → {reason.strip()}",
            allowed_mentions=NO_PINGS,
        )

    @discord.slash_command(name="modlogs", description="A user's last 25 moderation cases")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to look up")
    async def modlogs(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        cases = await mod_cases.list_cases(ctx.guild.id, user_id=user.id, limit=25)
        text = f"### 📜 Mod logs for {discord.utils.escape_markdown(str(user))}\n{_case_lines(cases) or 'No cases.'}"
        footer = "Struck-through cases are inactive (removed, cleared or expired)."
        await ctx.respond(view=reply_card(text[:3800], discord.Color.blurple(), footer), allowed_mentions=NO_PINGS)

    @discord.slash_command(name="modstats", description="Moderation actions per moderator")
    @discord.default_permissions(moderate_members=True)
    @discord.option("days", int, description="Look back this many days (default 30)", min_value=1, max_value=3650)
    async def modstats(self, ctx: discord.ApplicationContext, days: int = 30):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        since = discord.utils.utcnow() - timedelta(days=days)
        per_moderator: dict[int, Counter] = defaultdict(Counter)
        for moderator_id, action, count in await mod_cases.moderator_stats(ctx.guild.id, since):
            per_moderator[moderator_id][action] += count
        ranked = sorted(per_moderator.items(), key=lambda item: item[1].total(), reverse=True)[:25]
        lines = [
            f"<@{moderator_id}> **{counts.total()}** · " + ", ".join(f"{action} {n}" for action, n in counts.most_common())
            for moderator_id, counts in ranked
        ]
        text = f"### 📊 Moderator stats, last {days} days\n" + ("\n".join(lines) or "No moderation in that period.")
        await ctx.respond(view=reply_card(text[:3800], discord.Color.blurple()), allowed_mentions=NO_PINGS)

    # --- channels ----------------------------------------------------------------------------

    @discord.slash_command(name="lock", description="Stop @everyone from talking in a channel")
    @discord.default_permissions(moderate_members=True)
    @discord.option("channel", discord.TextChannel, description="Default: this channel", required=False)
    @discord.option("reason", str, description="Optional", max_length=MAX_REASON_LENGTH, required=False)
    async def lock(self, ctx: discord.ApplicationContext, channel: discord.TextChannel = None, reason: str = ""):
        if not await self._allowed(ctx, "mod.channels"):
            return
        channel = channel or ctx.channel
        if isinstance(channel, discord.Thread):
            return await ctx.respond("Threads can't be locked this way; lock the parent channel.", ephemeral=True)
        try:
            await mod_actions.lock_channel(
                channel, moderator_id=ctx.user.id, reason=f"{reason or 'No reason given'} (via command by {ctx.user.name})"
            )
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot can't edit that channel's permissions.", ephemeral=True)
        await ctx.respond(f"🔒 Locked {channel.mention}.")

    @discord.slash_command(name="unlock", description="Undo /lock on a channel")
    @discord.default_permissions(moderate_members=True)
    @discord.option("channel", discord.TextChannel, description="Default: this channel", required=False)
    async def unlock(self, ctx: discord.ApplicationContext, channel: discord.TextChannel = None):
        if not await self._allowed(ctx, "mod.channels"):
            return
        channel = channel or ctx.channel
        if isinstance(channel, discord.Thread):
            return await ctx.respond("Threads can't be unlocked this way; unlock the parent channel.", ephemeral=True)
        try:
            await mod_actions.unlock_channel(channel, reason=f"Unlocked (via command by {ctx.user.name})")
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot can't edit that channel's permissions.", ephemeral=True)
        await ctx.respond(f"🔓 Unlocked {channel.mention}.")

    @lockdown_group.command(name="start", description="Lock every public channel (or the configured list)")
    @discord.option("reason", str, description="Why", max_length=MAX_REASON_LENGTH)
    async def lockdown_start(self, ctx: discord.ApplicationContext, reason: str):
        if not await self._allowed(ctx, "mod.channels"):
            return

        async def run(interaction: discord.Interaction):
            locked, failed = await mod_actions.lockdown(
                ctx.guild, self.bot.settings, moderator_id=ctx.user.id, reason=f"{reason} (via command by {ctx.user.name})"
            )
            await interaction.followup.send(
                f"🔒 Lockdown: locked {locked} channel(s)" + (f", {failed} failed." if failed else ".")
            )

        await ctx.respond(
            view=ConfirmView("**Lock every public channel** (or the configured lockdown list)?", "Start lockdown", run),
            ephemeral=True,
        )

    @lockdown_group.command(name="end", description="Unlock every channel the bot locked")
    async def lockdown_end(self, ctx: discord.ApplicationContext):
        if not await self._allowed(ctx, "mod.channels"):
            return
        await ctx.defer()
        unlocked, failed = await mod_actions.end_lockdown(
            ctx.guild, reason=f"Lockdown ended (via command by {ctx.user.name})"
        )
        await ctx.respond(f"🔓 Lockdown over: unlocked {unlocked} channel(s)" + (f", {failed} failed." if failed else "."))

    @discord.slash_command(name="slowmode", description="Set a channel's slowmode")
    @discord.default_permissions(moderate_members=True)
    @discord.option("seconds", int, description="0 turns it off (max 21600 = 6h)", min_value=0, max_value=21600)
    @discord.option("channel", discord.TextChannel, description="Default: this channel", required=False)
    async def slowmode(self, ctx: discord.ApplicationContext, seconds: int, channel: discord.TextChannel = None):
        if not await self._allowed(ctx, "mod.channels"):
            return
        channel = channel or ctx.channel
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"/slowmode by {ctx.user.name}")
        except discord.HTTPException:
            return await ctx.respond("Discord refused: the bot can't edit that channel.", ephemeral=True)
        await ctx.respond(
            f"🐢 Slowmode in {channel.mention} is now {format_duration(seconds) if seconds else 'off'}."
        )

    # --- members -----------------------------------------------------------------------------

    @discord.slash_command(name="whois", description="Account info, roles and case counts for a user")
    @discord.default_permissions(moderate_members=True)
    @discord.option("user", discord.User, description="User to look up")
    async def whois(self, ctx: discord.ApplicationContext, user: discord.User):
        if not await self._allowed(ctx, "mod.cases.view"):
            return
        member = await resolve_member(ctx.guild, user.id)
        facts = [f"**ID** `{user.id}`", f"**Created** {discord.utils.format_dt(user.created_at, 'R')}"]
        lines = [f"### {discord.utils.escape_markdown(str(user))}"]
        if member is None:
            lines.append("Not in the server.")
        else:
            if member.joined_at:
                facts.append(f"**Joined** {discord.utils.format_dt(member.joined_at, 'R')}")
            if member.timed_out:
                facts.append(f"**Timed out** until {discord.utils.format_dt(member.communication_disabled_until, 'R')}")
        lines.append("　".join(facts))
        if member is not None:
            roles = " ".join(role.mention for role in reversed(member.roles) if not role.is_default()) or "None"
            if len(roles) > 1500:
                roles = roles[:1500].rsplit(" ", 1)[0] + " …"
            lines.append(f"**Roles** {roles}")
        counts = Counter(case.action for case in await mod_cases.list_cases(ctx.guild.id, user_id=user.id, limit=1000))
        lines.append("**Cases** " + (", ".join(f"{action} {n}" for action, n in counts.most_common()) or "None"))
        section = discord.ui.Section(
            discord.ui.TextDisplay("\n".join(lines)), accessory=discord.ui.Thumbnail(user.display_avatar.url)
        )
        view = discord.ui.DesignerView(discord.ui.Container(section, color=discord.Color.blurple()))
        await ctx.respond(view=view, allowed_mentions=NO_PINGS)

    # --- context menus -----------------------------------------------------------------------

    @discord.user_command(name="Mod logs")
    @discord.default_permissions(moderate_members=True)
    async def modlogs_menu(self, ctx: discord.ApplicationContext, user: discord.User):
        await self.modlogs.callback(self, ctx, user)

    @discord.user_command(name="Warn")
    @discord.default_permissions(moderate_members=True)
    async def warn_menu(self, ctx: discord.ApplicationContext, user: discord.User):
        if await self._allowed(ctx, "mod.warn"):
            await ctx.send_modal(
                ReasonModal(f"Warn {user.name}", lambda interaction, reason: self._perform(interaction, "warn", user.id, reason))
            )

    @discord.message_command(name="Delete & warn")
    @discord.default_permissions(moderate_members=True)
    async def delete_warn_menu(self, ctx: discord.ApplicationContext, message: discord.Message):
        if not await self._allowed(ctx, "mod.warn"):
            return
        excerpt = (message.content or "[no text]").replace("\n", " ")
        excerpt = excerpt if len(excerpt) <= 80 else excerpt[:79] + "…"

        async def submit(interaction: discord.Interaction, reason: str):
            # Warn first: if the hierarchy check refuses, the message stays.
            if await self._perform(interaction, "warn", message.author.id, f'{reason} (message: "{excerpt}")') is None:
                return
            try:
                await message.delete(reason=f"Delete & warn by {interaction.user.name}")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                await interaction.respond("Warned, but couldn't delete the message.", ephemeral=True)

        await ctx.send_modal(ReasonModal("Delete & warn", submit))

    @discord.message_command(name="Mark as scam image")
    @discord.default_permissions(moderate_members=True)
    async def mark_scam_image_menu(self, ctx: discord.ApplicationContext, message: discord.Message):
        if not await self._allowed(ctx, "mod.tools"):
            return
        await ctx.defer()
        hashes = [
            value
            for value in await asyncio.gather(
                *(scam_images.hash_attachment(a) for a in message.attachments if scam_images.is_hashable(a))
            )
            if value is not None
        ]
        if not hashes:
            return await self._private_error(ctx, "No usable images on that message.")
        try:
            hash_ids = [
                await scam_images.add(value, source="manual", added_by=ctx.user.id, note=f"message {message.id} by {message.author}")
                for value in hashes
            ]
        except Exception:
            log.exception("Couldn't save scam images from message %s", message.id)
            return await self._private_error(ctx, "Couldn't save, the database is unavailable.")
        deleted = "deleted the message"
        try:
            await message.delete(reason=f"Marked as a scam image by {ctx.user.name}")
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("Couldn't delete message %s after marking it as a scam image", message.id, exc_info=True)
            deleted = "couldn't delete the message"
        ids = ", ".join(f"#{hash_id}" for hash_id in hash_ids)
        await ctx.respond(f"🛡️ Added scam image {ids} and {deleted}.")

    # --- scam images -------------------------------------------------------------------------

    # --- tempban expiry ----------------------------------------------------------------------

    async def on_startup(self) -> None:
        if not self.expire_tempbans.is_running():
            self.expire_tempbans.start()

    async def on_shutdown(self) -> None:
        self.expire_tempbans.cancel()

    @tasks.loop(minutes=1)
    async def expire_tempbans(self):
        try:
            due = await mod_cases.due_expirations(discord.utils.utcnow())
        except Exception:
            log.exception("Couldn't load due tempbans")
            return
        for case in due:
            guild = self.bot.get_guild(case.guild_id)
            if guild is None:
                continue
            try:
                if case.action == "timeout":  # Discord already lifted it; the card flips to "expired"
                    await mod_actions.end_case(self.bot, case.guild_id, case.id, ended_by=None, note="expired")
                    continue
                if case.action != "ban":
                    continue
                try:
                    await mod_actions.perform(
                        self.bot,
                        guild,
                        action="unban",
                        target_id=case.user_id,
                        moderator=None,
                        reason=f"Tempban expired (case #{case.id})",
                        source="tempban",
                        notify=False,
                    )
                except ModActionError as error:
                    if error.status != 404:
                        raise
                    await mod_actions.end_case(self.bot, case.guild_id, case.id, ended_by=None)  # unbanned by hand
            except Exception:
                log.warning("Couldn't expire case #%s", case.id, exc_info=True)

    @expire_tempbans.before_loop
    async def _before_expire_tempbans(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(ModCommandsCog(bot))
