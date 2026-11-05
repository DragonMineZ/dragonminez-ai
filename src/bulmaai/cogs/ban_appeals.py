"""Staff side of the appeals website: Accept / Deny / Ask a question on the appeal cards (ui/appeal_cards.py).
Clicks and form submits are routed by custom id, so they work after a restart. Moderator+ in the main guild only.
Accept and Deny claim the row first (WHERE outcome IS NULL, first click wins); Accept then lifts the ban or timeout
through mod_actions.perform, and a failure gives the claim back."""

import logging

import discord
from discord.ext import commands

from bulmaai.services import ban_appeals as store
from bulmaai.services import mod_actions
from bulmaai.ui import appeal_cards
from bulmaai.ui.v2 import unbusy, while_busy
from bulmaai.utils.i18n import reader_language, t, table
from bulmaai.web.core import PERMISSIONS, tier_for

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
# Replies to the clicking moderator, in their language; the appeal card itself stays English.
TEXT = table({
    "en": {
        "handled": "Someone already handled this appeal.",
        "failed": "Something went wrong, the appeal may not have been answered.",
        "denied": "Only moderators can handle appeals.",
        "empty_question": "The question can't be empty.",
        "done": "Done.",
    },
    "es": {
        "handled": "Alguien ya se encargó de esta apelación.",
        "failed": "Algo salió mal, puede que la apelación no se haya respondido.",
        "denied": "Solo los moderadores pueden gestionar apelaciones.",
        "empty_question": "La pregunta no puede estar vacía.",
        "done": "Listo.",
    },
    "pt": {
        "handled": "Alguém já cuidou deste recurso.",
        "failed": "Algo deu errado, o recurso pode não ter sido respondido.",
        "denied": "Só moderadores podem cuidar de recursos.",
        "empty_question": "A pergunta não pode ficar vazia.",
        "done": "Feito.",
    },
})
HANDLED_MSG = TEXT["en"]["handled"]


class BanAppealsCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._working: set[int] = set()  # cards whose button click is still being handled (ui.v2.while_busy)
        if self.settings.appeals_channel_id and not store.login_ready(self.settings):  # once, at load
            log.error(
                "The appeals website is on but can't log anyone in: set DISCORD_OAUTH_CLIENT_SECRET and "
                "PANEL_SESSION_SECRET (and the Discord OAuth client id)",
                extra={"event": "ban_appeals_not_set_up"},
            )

    @property
    def settings(self):
        return self.bot.settings

    def _say(self, interaction: discord.Interaction, key: str) -> str:
        return t(TEXT, key, reader_language(interaction, self.settings))

    def _allowed(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        return (
            guild is not None
            and guild.id == self.settings.panel_guild_id
            and tier_for(interaction.user, self.settings) >= PERMISSIONS["mod.ban"]
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id")
        if interaction.type == discord.InteractionType.modal_submit:
            parsed = appeal_cards.parse_form_id(custom_id)
            handler = parsed and {"deny": self._deny_submit, "ask": self._ask_submit}[parsed[0]]
        elif interaction.type == discord.InteractionType.component:
            parsed = appeal_cards.parse_custom_id(custom_id)
            handler = parsed and {"accept": self._accept, "deny": self._deny, "ask": self._ask}[parsed[0]]
        else:
            return
        if not handler:
            return
        if not self._allowed(interaction):
            return await interaction.response.send_message(self._say(interaction, "denied"), ephemeral=True)
        try:
            await handler(interaction, parsed[1])
        except Exception:
            log.exception("Appeal interaction failed", extra={"event": "ban_appeal_interaction_failed", "custom_id": custom_id})
            if not interaction.response.is_done():
                await interaction.response.send_message(self._say(interaction, "failed"), ephemeral=True)

    async def _show_current(self, interaction: discord.Interaction, appeal_id: int) -> None:
        """A click on an appeal someone else decided: the card shows their outcome, the clicker hears why."""
        appeal = await store.get(appeal_id)
        view = await store.render(self.bot, appeal) if appeal is not None else None
        handled = self._say(interaction, "handled")
        if interaction.response.is_done():
            if view is not None:
                await interaction.edit_original_response(view=view)
            else:
                await unbusy(interaction)
            await interaction.followup.send(handled, ephemeral=True)
        elif view is not None and getattr(interaction, "message", None) is not None:
            await interaction.response.edit_message(view=view)
            await interaction.followup.send(handled, ephemeral=True)
        else:
            await interaction.response.send_message(handled, ephemeral=True)

    # --- Accept ---

    async def _accept(self, interaction: discord.Interaction, appeal_id: int) -> None:
        async def work() -> bool:
            await self.accept(interaction, appeal_id)
            return True

        await while_busy(interaction, self._working, work, failed=self._say(interaction, "failed"))

    async def accept(self, interaction: discord.Interaction, appeal_id: int) -> None:
        staff = interaction.user
        appeal = await store.claim(appeal_id, "accepted", staff.id)
        if appeal is None:
            return await self._show_current(interaction, appeal_id)
        guild = self.bot.get_guild(self.settings.panel_guild_id)
        try:
            if guild is None:
                raise mod_actions.ModActionError("The bot can't reach the server right now.", 503)
            await mod_actions.perform(
                self.bot,
                guild,
                action="unban" if appeal.kind == "ban" else "untimeout",
                target_id=appeal.user_id,
                moderator=staff,
                reason=f"Appeal #{appeal.id} accepted",
                source="appeal",
            )
        except mod_actions.ModActionError as error:
            if error.status != 404:  # 404: already unbanned, or no longer in the server; the appeal still stands
                await store.release(appeal_id, staff.id)
                await unbusy(interaction)
                language = reader_language(interaction, self.settings)
                return await interaction.followup.send(error.text(language), ephemeral=True)
        except Exception:
            await store.release(appeal_id, staff.id)
            raise
        await interaction.edit_original_response(view=await store.render(self.bot, appeal))

    # --- Deny ---

    async def _deny(self, interaction: discord.Interaction, appeal_id: int) -> None:
        appeal = await store.get(appeal_id)
        if appeal is None or appeal.outcome is not None:
            return await self._show_current(interaction, appeal_id)
        language = reader_language(interaction, self.settings)
        await interaction.response.send_modal(appeal_cards.deny_modal(appeal_id, language))

    async def _deny_submit(self, interaction: discord.Interaction, appeal_id: int) -> None:
        reason = appeal_cards.read_text((interaction.data or {}).get("components"))[:500]
        appeal = await store.claim(appeal_id, "denied", interaction.user.id, reason)
        if appeal is None:
            return await self._show_current(interaction, appeal_id)
        guild = self.bot.get_guild(self.settings.panel_guild_id)
        try:
            if guild is not None:  # a note case: the weekly digest counts denials from it
                await mod_actions.perform(
                    self.bot,
                    guild,
                    action="note",
                    target_id=appeal.user_id,
                    moderator=interaction.user,
                    reason=f"Appeal denied: {reason or 'no reason given'}",
                    source="appeal",
                )
        except Exception:
            log.warning("Couldn't record the denial of appeal #%s as a case", appeal_id, exc_info=True)
        await self._update_card(interaction, appeal)

    # --- Ask a question ---

    async def _ask(self, interaction: discord.Interaction, appeal_id: int) -> None:
        appeal = await store.get(appeal_id)
        if appeal is None or appeal.outcome is not None:
            return await self._show_current(interaction, appeal_id)
        language = reader_language(interaction, self.settings)
        await interaction.response.send_modal(appeal_cards.ask_modal(appeal_id, language))

    async def _ask_submit(self, interaction: discord.Interaction, appeal_id: int) -> None:
        question = appeal_cards.read_text((interaction.data or {}).get("components"))[: store.QUESTION_MAX]
        if not question:
            return await interaction.response.send_message(self._say(interaction, "empty_question"), ephemeral=True)
        appeal = await store.ask(appeal_id, question, interaction.user.id)
        if appeal is None:
            return await self._show_current(interaction, appeal_id)
        await self._update_card(interaction, appeal)

    async def _update_card(self, interaction: discord.Interaction, appeal: store.Appeal) -> None:
        view = await store.render(self.bot, appeal)
        if getattr(interaction, "message", None) is not None:
            await interaction.response.edit_message(view=view)
            return
        await interaction.response.defer(ephemeral=True, invisible=False)
        message = await store.card_message(self.bot, appeal)
        if message is not None:
            await message.edit(view=view, allowed_mentions=NO_MENTIONS)
        await interaction.followup.send(self._say(interaction, "done"), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(BanAppealsCog(bot))
