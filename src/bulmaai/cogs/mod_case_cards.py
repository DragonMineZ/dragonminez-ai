"""The Edit / Undo / History buttons on mod-log case cards (ui/mod_cards.py). The custom id carries the case,
modcase:<edit|undo|history>:<case_id>, so the buttons keep working after a restart. The card itself changing
is the confirmation for edit and undo; errors and history are ephemeral."""

import logging

import discord
from discord.ext import commands

from bulmaai.services import mod_actions, mod_cases
from bulmaai.services.mod_actions import ModActionError
from bulmaai.ui.mod_cards import CASE
from bulmaai.ui.v2 import unbusy, while_busy
from bulmaai.utils.i18n import reader_language, t, table
from bulmaai.web.core import PERMISSIONS, resolve_member, tier_for

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
UNDO_PERMISSIONS = {"warn": "mod.cases.remove", "timeout": "mod.timeout", "ban": "mod.ban"}
UNDO_ACTIONS = {"timeout": "untimeout", "ban": "unban"}
HISTORY_LIMIT = 15
# Everything here answers the clicking moderator alone, so it is in their language.
TEXT = table({
    "en": {
        "edit_title": "Edit reason, case #{case_id}",
        "reason": "Reason",
        "failed": "Something went wrong, the bot logs have the details.",
        "main_only": "That only works on the DragonMineZ server.",
        "tier_denied": "Your staff tier can't do that.",
        "gone": "Case #{case_id} no longer exists.",
        "empty_reason": "The reason can't be empty.",
        "cant_undo": "That case can't be undone.",
        "already_ended": "Case #{case_id} was already ended.",
        "undo_failed": "Something went wrong undoing that case.",
        "history": "**Last {count} cases for <@{user_id}>**",
        "no_cases": "No cases.",
    },
    "es": {
        "edit_title": "Editar motivo, caso #{case_id}",
        "reason": "Motivo",
        "failed": "Algo salió mal; los logs del bot tienen los detalles.",
        "main_only": "Eso solo funciona en el servidor de DragonMineZ.",
        "tier_denied": "Tu nivel de staff no puede hacer eso.",
        "gone": "El caso #{case_id} ya no existe.",
        "empty_reason": "El motivo no puede estar vacío.",
        "cant_undo": "Ese caso no se puede deshacer.",
        "already_ended": "El caso #{case_id} ya había terminado.",
        "undo_failed": "Algo salió mal al deshacer ese caso.",
        "history": "**Últimos {count} casos de <@{user_id}>**",
        "no_cases": "Sin casos.",
    },
    "pt": {
        "edit_title": "Editar motivo, caso #{case_id}",
        "reason": "Motivo",
        "failed": "Algo deu errado; os logs do bot têm os detalhes.",
        "main_only": "Isso só funciona no servidor do DragonMineZ.",
        "tier_denied": "Seu nível de staff não pode fazer isso.",
        "gone": "O caso #{case_id} não existe mais.",
        "empty_reason": "O motivo não pode ficar vazio.",
        "cant_undo": "Esse caso não pode ser desfeito.",
        "already_ended": "O caso #{case_id} já tinha sido encerrado.",
        "undo_failed": "Algo deu errado ao desfazer esse caso.",
        "history": "**Últimos {count} casos de <@{user_id}>**",
        "no_cases": "Nenhum caso.",
    },
})


class ReasonModal(discord.ui.Modal):
    """One long text box for the new reason; only lives while the moderator has it open."""

    def __init__(self, case: mod_cases.ModCase, on_submit, language: str = "en"):
        title = t(TEXT, "edit_title", language, case_id=case.id)
        super().__init__(title=title[:45], custom_id=f"{CASE}-edit:{case.id}")
        self.reason = discord.ui.InputText(
            label=t(TEXT, "reason", language),
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
        await _reply(interaction, t(TEXT, "failed", reader_language(interaction)))
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
        self._working: set[int] = set()  # messages whose button click is still being handled (ui.v2.while_busy)

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

    def _say(self, interaction: discord.Interaction, key: str, **values) -> str:
        return t(TEXT, key, reader_language(interaction, self.settings), **values)

    async def _staff(self, interaction: discord.Interaction, permission: str) -> bool:
        guild = interaction.guild
        if guild is None or guild.id != self.settings.panel_guild_id:
            await _reply(interaction, self._say(interaction, "main_only"))
            return False
        if tier_for(interaction.user, self.settings) < PERMISSIONS[permission]:
            await _reply(interaction, self._say(interaction, "tier_denied"))
            return False
        return True

    async def _load(self, interaction: discord.Interaction, case_id: int) -> mod_cases.ModCase | None:
        case = await mod_cases.get_case(interaction.guild.id, case_id)
        if case is None:
            await _reply(interaction, self._say(interaction, "gone", case_id=case_id))
        return case

    async def _outranks(self, interaction: discord.Interaction, user_id: int) -> bool:
        target = await resolve_member(interaction.guild, user_id)
        try:
            mod_actions.check_hierarchy(
                self.bot, interaction.guild, interaction.user, user_id, target, discord_action=False
            )
        except ModActionError as error:
            await _reply(interaction, error.text(reader_language(interaction, self.settings)))
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
                await _reply(modal_interaction, self._say(modal_interaction, "empty_reason"))
                return
            await mod_cases.update_reason(case.guild_id, case_id, reason)
            await modal_interaction.response.defer()
            if fresh := await mod_cases.get_case(case.guild_id, case_id):
                await mod_actions.refresh_case_card(self.bot, fresh)

        await interaction.response.send_modal(ReasonModal(case, submit, reader_language(interaction, self.settings)))

    async def _undo(self, interaction: discord.Interaction, case_id: int) -> None:
        case = await mod_cases.get_case(interaction.guild.id, case_id) if interaction.guild else None
        permission = UNDO_PERMISSIONS.get(case.action) if case else None
        if permission is None:
            await _reply(interaction, self._say(interaction, "cant_undo"))
            return
        if not await self._staff(interaction, permission) or not await self._outranks(interaction, case.user_id):
            return
        if not case.active:
            await _reply(interaction, self._say(interaction, "already_ended", case_id=case_id))
            return
        async def undo() -> None:  # ending the case re-renders this card
            try:
                if case.action == "warn":
                    if await mod_actions.end_case(self.bot, case.guild_id, case_id, ended_by=interaction.user.id) is None:
                        await _reply(interaction, self._say(interaction, "already_ended", case_id=case_id))
                        return await unbusy(interaction)
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
                await _reply(interaction, error.text(reader_language(interaction, self.settings)))
                return await unbusy(interaction)
            if interaction.message.id != case.log_message_id:  # a /case copy: the refresh only reached the mod-log card
                fresh = await mod_cases.get_case(case.guild_id, case_id) or case
                await interaction.edit_original_response(view=await mod_actions.render_case_card(self.bot, fresh))

        await while_busy(interaction, self._working, undo, failed=self._say(interaction, "undo_failed"))

    async def _history(self, interaction: discord.Interaction, case_id: int) -> None:
        if not await self._staff(interaction, "mod.cases.view"):
            return
        case = await self._load(interaction, case_id)
        if case is None:
            return
        cases = await mod_cases.list_cases(case.guild_id, user_id=case.user_id, limit=HISTORY_LIMIT)
        if cases:
            text = f"{self._say(interaction, 'history', count=len(cases), user_id=case.user_id)}\n{_history_lines(cases)}"
        else:
            text = self._say(interaction, "no_cases")
        await _reply(interaction, text)


def setup(bot: discord.Bot):
    bot.add_cog(ModCaseCardsCog(bot))
