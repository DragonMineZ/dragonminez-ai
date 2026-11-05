"""The MAIL_USER mailbox in an admin channel (services/mail_inbox.py, ui/mail_cards.py). A loop reads new mail every
2 minutes and posts each as a card; an admin's Reply opens a form, and the submit claims the row (first wins), sends
the answer over SMTP from the same address and turns the card into its replied state. A failed send gives the claim
back. Clicks and submits are routed by custom id, so they work after a restart. Main-guild admins only."""

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from bulmaai.services import mail_inbox as store
from bulmaai.ui import mail_cards
from bulmaai.ui.v2 import trim
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import is_admin

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
DENIED_MSG = "Only admins can answer mail."
GONE_MSG = "This mail is gone."
REPLIED_MSG = "Someone already replied to this mail."
FAILED_MSG = "Something went wrong, the reply may not have been sent."
OFF_MSG = "The mail inbox isn't set up."


class MailInboxCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._read_failing = False  # WARNING on the first failure in a row only (WARNING+ reaches staff)
        self._post_failing = False

    @property
    def settings(self):
        return self.bot.settings

    async def on_startup(self) -> None:
        if not self.poll.is_running():
            self.poll.start()

    async def on_shutdown(self) -> None:
        self.poll.cancel()

    # --- polling ---

    @tasks.loop(minutes=2)
    async def poll(self) -> None:
        try:
            await self._poll()
        except Exception:
            log.exception("Mail inbox poll failed")

    @poll.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    def _reason(self, error: Exception) -> str:
        text = f"{type(error).__name__}: {error}"
        if password := self.settings.mail_password:
            text = text.replace(password, "***")
        return trim(text, 200)

    async def _poll(self) -> None:
        settings = self.settings
        if not store.enabled(settings):
            return
        known = await store.known_ids()
        try:
            raws = await asyncio.to_thread(
                store.fetch_new, settings.mail_imap_host, settings.mail_user, settings.mail_password, known
            )
        except Exception as error:
            level = logging.INFO if self._read_failing else logging.WARNING
            self._read_failing = True
            log.log(level, "Couldn't read the mailbox: %s", self._reason(error), extra={"event": "mail_read_failed"})
            return
        if self._read_failing:
            log.info("Reading the mailbox works again")
        self._read_failing = False
        own = settings.mail_user.lower()
        for raw in raws:
            incoming = store.parse_message(raw)
            if incoming is None or incoming.from_addr.lower() == own:
                continue
            mail = await store.insert(incoming)
            if mail is not None:
                await self._post(mail)

    async def _channel(self, channel_id: int | None):
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel if hasattr(channel, "send") else None

    async def _post(self, mail: store.Mail) -> None:
        """Posts the card; on failure the row goes so the next poll tries again."""
        try:
            channel = await self._channel(self.settings.mail_channel_id)
            if channel is None:
                raise RuntimeError("the mail channel can't be reached")
            message = await channel.send(view=mail_cards.mail_card(mail), allowed_mentions=NO_MENTIONS)
            await store.set_card(mail.id, channel.id, message.id)
        except Exception as error:
            await store.delete(mail.id)
            level = logging.INFO if self._post_failing else logging.WARNING
            self._post_failing = True
            log.log(level, "Couldn't post mail #%s: %s", mail.id, self._reason(error), extra={"event": "mail_post_failed"})
            return
        self._post_failing = False

    # --- interactions ---

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id")
        if interaction.type == discord.InteractionType.modal_submit:
            parsed, handler = mail_cards.parse_form_id(custom_id), self._submit
        elif interaction.type == discord.InteractionType.component:
            parsed, handler = mail_cards.parse_custom_id(custom_id), self._click
        else:
            return
        if not parsed:
            return
        if not is_admin(interaction.user, settings=self.settings):
            return await interaction.response.send_message(DENIED_MSG, ephemeral=True)
        try:
            await handler(interaction, parsed[1])
        except Exception:
            log.exception("Mail interaction failed", extra={"event": "mail_interaction_failed", "custom_id": custom_id})
            if not interaction.response.is_done():
                await interaction.response.send_message(FAILED_MSG, ephemeral=True)
            else:
                await interaction.followup.send(FAILED_MSG, ephemeral=True)

    async def _show_replied(self, interaction: discord.Interaction, mail_id: int) -> None:
        """Someone else replied first: the card shows their reply, the clicker hears why."""
        mail = await store.get(mail_id)
        if mail is None:
            return await interaction.response.send_message(GONE_MSG, ephemeral=True)
        if getattr(interaction, "message", None) is not None:
            await interaction.response.edit_message(view=mail_cards.mail_card(mail))
            await interaction.followup.send(REPLIED_MSG, ephemeral=True)
        else:
            await interaction.response.send_message(REPLIED_MSG, ephemeral=True)

    async def _click(self, interaction: discord.Interaction, mail_id: int) -> None:
        mail = await store.get(mail_id)
        if mail is None:
            return await interaction.response.send_message(GONE_MSG, ephemeral=True)
        if mail.replied_at is not None:
            return await self._show_replied(interaction, mail_id)
        await interaction.response.send_modal(mail_cards.reply_modal(mail, self.settings.mail_user))

    async def _submit(self, interaction: discord.Interaction, mail_id: int) -> None:
        settings = self.settings
        if not store.enabled(settings):
            return await interaction.response.send_message(OFF_MSG, ephemeral=True)
        text = mail_cards.read_text((interaction.data or {}).get("components"))[: mail_cards.REPLY_MAX]
        if not text:
            return await interaction.response.send_message("The reply can't be empty.", ephemeral=True)
        staff_id = interaction.user.id
        mail = await store.claim(mail_id, staff_id, text)
        if mail is None:
            return await self._show_replied(interaction, mail_id)
        await interaction.response.defer(ephemeral=True, invisible=False)  # SMTP can take a while
        try:
            message = store.build_reply(mail, settings.mail_user, text)
            await asyncio.to_thread(
                store.send_reply, settings.mail_smtp_host, settings.mail_user, settings.mail_password, message
            )
        except Exception as error:
            await store.release(mail_id, staff_id)
            reason = self._reason(error)
            log.warning("Couldn't send the reply to mail #%s: %s", mail_id, reason, extra={"event": "mail_send_failed"})
            return await interaction.followup.send(f"Couldn't send the reply: {reason}", ephemeral=True)
        await self._update_card(interaction, mail)
        await interaction.followup.send("Sent.", ephemeral=True)

    async def _update_card(self, interaction: discord.Interaction, mail: store.Mail) -> None:
        """The reply went out; a card that can't be edited is only logged."""
        try:
            message = getattr(interaction, "message", None)
            if message is None:
                channel = await self._channel(mail.card_channel_id)
                if channel is None or mail.card_message_id is None:
                    return
                message = await channel.fetch_message(mail.card_message_id)
            await message.edit(view=mail_cards.mail_card(mail), allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            log.info("Couldn't update the card of mail #%s", mail.id, exc_info=True)


def setup(bot: discord.Bot):
    bot.add_cog(MailInboxCog(bot))
