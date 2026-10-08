"""The Edit / Undo / History buttons on mod-log case cards (ui/mod_cards.py). The custom id carries the case,
modcase:<edit|undo|history>:<case_id>, so the buttons keep working after a restart. The card itself changing
is the confirmation for edit and undo; errors and history are ephemeral."""

import logging

import discord
from discord.ext import commands

from bulmaai.services import mod_actions, mod_cases
from bulmaai.services.mod_actions import ModActionError
from bulmaai.ui.mod_cards import CASE
from bulmaai.web.core import PERMISSIONS, resolve_member, tier_for

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
UNDO_PERMISSIONS = {"warn": "mod.cases.remove", "timeout": "mod.timeout", "ban": "mod.ban"}
UNDO_ACTIONS = {"timeout": "untimeout", "ban": "unban"}
HISTORY_LIMIT = 15


class ReasonModal(discord.ui.Modal):
    """One long text box for the new reason; only lives while the moderator has it open."""

    def __init__(self, case: mod_cases.ModCase, on_submit):
        super().__init__(title=f"Edit reason, case #{case.id}"[:45], custom_id=f"{CASE}-edit:{case.id}")
        self.reason = discord.ui.InputText(
            label="Reason",
            style=discord.InputTextStyle.long,
            max_length=400,
            value=(case.reason or "")[:400] or None,
        )
        self.add_item(self.reason)
        self.on_submit = on_submit

    async def callback(self, interaction: discord.Interaction):
        await self.on_submit(interaction, (self.reason.value or "").strip())

    async def on_error(self, error: Exception, interaction: discord.Interaction) -> None:
        await _fail(interaction, error, self.custom_id)


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def _fail(interaction: discord.Interaction, error: Exception, custom_id: str | None) -> None:
    log.error(
        "Case card interaction failed",
        exc_info=error,
        extra={"event": "mod_case_card_failed", "custom_id": custom_id, "user_id": interaction.user.id},
    )
    try:
        await _reply(interaction, "Something went wrong, the bot logs have the details.")
    except discord.HTTPException:
        pass


def _history_lines(cases: list[mod_cases.ModCase]) -> str:
    lines = []
    for case in cases:
        reason = (case.reason or "no reason").replace("\n", " ")
        reason = discord.utils.escape_markdown(reason if len(reason) <= 60 else reason[:59] + "…")
        line = f"`#{case.id}` **{case.action}** · {case.source} · {discord.utils.format_dt(case.created_at, 'R')} · {reason}"
        lines.append(line if case.active else f"~~{line}~~")
    return "\n".join(lines)[:2000]


class ModCaseCardsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @property
    def settings(self):
        return self.bot.settings

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id") or ""
        prefix, _, rest = custom_id.partition(":")
        if prefix != CASE:
            return
        action, _, case_id = rest.partition(":")
        handler = {"edit": self._edit, "undo": self._undo, "history": self._history}.get(action)
        if handler is None or not case_id.isdigit():
            return
        try:
            await handler(interaction, int(case_id))
        except Exception as error:
            await _fail(interaction, error, custom_id)

    async def _staff(self, interaction: discord.Interaction, permission: str) -> bool:
        guild = interaction.guild
        if guild is None or guild.id != self.settings.panel_guild_id:
            await _reply(interaction, "That only works on the DragonMineZ server.")
            return False
        if tier_for(interaction.user, self.settings) < PERMISSIONS[permission]:
            await _reply(interaction, "Your staff tier can't do that.")
            return False
        return True

    async def _load(self, interaction: discord.Interaction, case_id: int) -> mod_cases.ModCase | None:
        case = await mod_cases.get_case(interaction.guild.id, case_id)
        if case is None:
            await _reply(interaction, f"Case #{case_id} no longer exists.")
        return case

    async def _outranks(self, interaction: discord.Interaction, user_id: int) -> bool:
        target = await resolve_member(interaction.guild, user_id)
        try:
            mod_actions.check_hierarchy(
                self.bot, interaction.guild, interaction.user, user_id, target, discord_action=False
            )
        except ModActionError as error:
            await _reply(interaction, str(error))
            return False
        return True

    async def _edit(self, interaction: discord.Interaction, case_id: int) -> None:
        if not await self._staff(interaction, "mod.cases.edit"):
            return
        case = await self._load(interaction, case_id)
        if case is None or not await self._outranks(interaction, case.user_id):
            return

        async def submit(modal_interaction: discord.Interaction, reason: str) -> None:
            if not reason:
                await _reply(modal_interaction, "The reason can't be empty.")
                return
            await mod_cases.update_reason(case.guild_id, case_id, reason)
            await modal_interaction.response.defer()
            if fresh := await mod_cases.get_case(case.guild_id, case_id):
                await mod_actions.refresh_case_card(self.bot, fresh)

        await interaction.response.send_modal(ReasonModal(case, submit))

    async def _undo(self, interaction: discord.Interaction, case_id: int) -> None:
        case = await mod_cases.get_case(interaction.guild.id, case_id) if interaction.guild else None
        permission = UNDO_PERMISSIONS.get(case.action) if case else None
        if permission is None:
            await _reply(interaction, "That case can't be undone.")
            return
        if not await self._staff(interaction, permission) or not await self._outranks(interaction, case.user_id):
            return
        if not case.active:
            await _reply(interaction, f"Case #{case_id} was already ended.")
            return
        await interaction.response.defer()
        try:
            if case.action == "warn":
                if await mod_actions.end_case(self.bot, case.guild_id, case_id, ended_by=interaction.user.id) is None:
                    await _reply(interaction, f"Case #{case_id} was already ended.")
            else:
                await mod_actions.perform(
                    self.bot,
                    interaction.guild,
                    action=UNDO_ACTIONS[case.action],
                    target_id=case.user_id,
                    moderator=interaction.user,
                    reason=f"Undone from case #{case_id}",
                    source="case card",
                )
        except ModActionError as error:
            await _reply(interaction, str(error))

    async def _history(self, interaction: discord.Interaction, case_id: int) -> None:
        if not await self._staff(interaction, "mod.cases.view"):
            return
        case = await self._load(interaction, case_id)
        if case is None:
            return
        cases = await mod_cases.list_cases(case.guild_id, user_id=case.user_id, limit=HISTORY_LIMIT)
        text = f"**Last {len(cases)} cases for <@{case.user_id}>**\n{_history_lines(cases)}" if cases else "No cases."
        await _reply(interaction, text)


def setup(bot: discord.Bot):
    bot.add_cog(ModCaseCardsCog(bot))
