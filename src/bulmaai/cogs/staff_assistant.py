"""Staff Bulma in Discord: /bulma (ask, settings, forget, export, connect), the onboarding form, confirm cards for the
actions she proposes, and the sweeps (expired cards, reminders, memory distilling). Logic lives in services/staff_*."""

import asyncio
import io
import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from bulmaai.cogs.ai_tickets import (
    _card_text,
    _has_support_request_content,
    _is_pinging_bot,
    _is_ticket_channel,
    _strip_bot_mentions,
)

from bulmaai.services import ai_budget, staff_assistant, staff_store, staff_tools
from bulmaai.ui import staff_assistant as ui
from bulmaai.ui.v2 import already_working, card, while_busy
from bulmaai.utils.i18n import described, reader_language, t, table
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.web.core import Tier, resolve_member, tier_for
from bulmaai.services.mod_actions import ModActionError

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
LEARN_CHANNELS = 40
LEARN_PER_CHANNEL = 200
LEARN_MESSAGES = 100
HISTORY_MESSAGES = 10
COMMAND_PREFIXES = ("!", "/", ".")
CONTEXTS = {discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm}

DESC = table({
    "en": {
        "bulma": "Staff Bulma, your private assistant",
        "ask": "Ask Bulma anything",
        "prompt": "What do you need?",
        "settings": "Change your Bulma settings",
        "forget": "Make Bulma forget everything about you",
        "export": "Get a copy of what Bulma keeps about you",
        "connect": "Connect Claude (MCP) to Bulma",
    },
    "es": {
        "bulma": "Bulma para staff, tu asistente privada",
        "ask": "Pregúntale lo que quieras a Bulma",
        "prompt": "¿Qué necesitas?",
        "settings": "Cambia tus ajustes de Bulma",
        "forget": "Haz que Bulma olvide todo sobre ti",
        "export": "Obtén una copia de lo que Bulma guarda de ti",
        "connect": "Conecta Claude (MCP) con Bulma",
    },
    "pt": {
        "bulma": "Bulma para staff, sua assistente privada",
        "ask": "Pergunte qualquer coisa à Bulma",
        "prompt": "Do que você precisa?",
        "settings": "Altere seus ajustes da Bulma",
        "forget": "Faça a Bulma esquecer tudo sobre você",
        "export": "Receba uma cópia do que a Bulma guarda sobre você",
        "connect": "Conecte o Claude (MCP) à Bulma",
    },
})


def export_payload(profile: dict | None, notes: list[dict], reminders: list[dict], vault: list[dict]) -> bytes:
    data = {"profile": profile, "notes": notes, "reminders": reminders, "vault": vault}
    return json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")


class StaffAssistantCog(ReloadableCog):
    bulma = discord.SlashCommandGroup("bulma", **described(DESC, "bulma"), contexts=CONTEXTS)

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._working: set[int] = set()
        self._onboarded: set[int] = set()  # cache of staff_profiles.user_id; the table is the truth
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def settings(self):
        return self.bot.settings

    async def on_startup(self) -> None:
        try:
            self._onboarded = set(await staff_store.list_profile_ids())
        except Exception:
            log.exception("Couldn't load the Staff Bulma profile ids")
        for loop in (self.sweep_actions, self.send_reminders, self.distill_all):
            if not loop.is_running():
                loop.start()

    async def on_shutdown(self) -> None:
        for loop in (self.sweep_actions, self.send_reminders, self.distill_all):
            loop.cancel()

    # --- gate ---

    async def _staff_member(self, interaction: discord.Interaction) -> discord.Member | None:
        """The interaction's user as a main-guild member of tier HELPER+ with Staff Bulma on, else None (also from DMs)."""
        return await self._gate(interaction.user, getattr(interaction, "guild_id", None))

    async def _gate(self, user, guild_id: int | None) -> discord.Member | None:
        settings = self.settings
        if not getattr(settings, "staff_ai_enabled", True):
            return None
        if guild_id is not None and guild_id != settings.panel_guild_id:
            return None
        guild = self.bot.get_guild(settings.panel_guild_id)
        if guild is None:
            return None
        member = user if isinstance(user, discord.Member) and user.guild.id == guild.id else await resolve_member(guild, user.id)
        if member is None or tier_for(member, settings) < Tier.HELPER:
            return None
        return member

    async def _language(self, member: discord.Member) -> str:
        return staff_assistant.language_for(member, await staff_store.get_profile(member.id), self.settings)

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(t(ui.TEXT, "denied", reader_language(interaction)), ephemeral=True)

    # --- answering (shared with the DM / ping path) ---

    async def answer_text(
        self, member: discord.Member, text: str, history: list[dict], *, origin: str, channel_id: int | None
    ) -> tuple[staff_assistant.StaffReply, list[dict]]:
        """Asks Staff Bulma and loads the pending action rows her reply proposes. Sending is the caller's job."""
        reply = await staff_assistant.ask(
            bot=self.bot, member=member, text=text, history=history, origin=origin, channel_id=channel_id
        )
        rows = []
        for action_id in reply.pending_action_ids:
            row = await staff_store.get_action(action_id)
            if row is not None:
                rows.append(row)
        return reply, rows

    async def send_confirm_dm(self, member: discord.Member, action_id: int) -> bool:
        """DMs the confirm card of a pending action and remembers the message (for the expiry sweep)."""
        row = await staff_store.get_action(action_id)
        if row is None or row["outcome"] is not None:
            return False
        try:
            message = await member.send(
                view=ui.confirm_view(row, await self._language(member)), allowed_mentions=NO_MENTIONS
            )
            await staff_store.set_action_card(action_id, message.channel.id, message.id)
        except discord.HTTPException:
            log.info("Couldn't DM a confirm card to %s", member.id, exc_info=True)
            return False
        return True

    # --- vault: what onboarded staff write where members can read it ---

    def _member_role(self, guild: discord.Guild | None) -> discord.Role | None:
        role_id = getattr(self.settings, "member_role_id", None)
        return guild.get_role(role_id) if guild is not None and role_id else None

    def _is_ticket(self, channel) -> bool:
        if _is_ticket_channel(channel, settings=self.settings):
            return True
        closed = getattr(self.settings, "ticket_closed_category_id", None)
        return closed is not None and getattr(channel, "category_id", None) == closed

    def _visible_to_members(self, channel, guild: discord.Guild | None) -> bool:
        """True when the Member role can see the channel (a thread follows its parent; private threads never count).
        No Member role found = False, so nothing is stored."""
        if isinstance(channel, discord.Thread):
            if channel.type == discord.ChannelType.private_thread:
                return False
            channel = channel.parent
        if channel is None or self._is_ticket(channel):
            return False
        role = self._member_role(guild)
        if role is None:
            return False
        return bool(channel.permissions_for(role).view_channel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._capture(message)
        except Exception:
            log.info("Staff vault capture failed", exc_info=True)
        try:
            if await self.handles(message):
                await self._answer_message(message)
        except Exception:
            log.exception("Staff Bulma couldn't answer a message")

    async def _capture(self, message: discord.Message) -> None:
        guild = message.guild
        author = message.author
        if (
            guild is None
            or guild.id != self.settings.panel_guild_id
            or author.id not in self._onboarded
            or author.bot
            or getattr(message, "webhook_id", None)
            or not (message.content or "").strip()
            or not self._visible_to_members(message.channel, guild)
        ):
            return
        await staff_store.store_message(
            message.id, author.id, guild.id, message.channel.id, message.content, message.created_at
        )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        cached = payload.cached_message
        if cached is not None and cached.author.id not in self._onboarded:
            return
        content = (payload.data or {}).get("content")
        if content is None:
            return
        try:
            await staff_store.edit_message(payload.message_id, content, datetime.now(timezone.utc))
        except Exception:
            log.info("Staff vault edit failed", exc_info=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        cached = payload.cached_message
        if cached is not None and cached.author.id not in self._onboarded:
            return
        try:
            await staff_store.delete_message(payload.message_id)
        except Exception:
            log.info("Staff vault delete failed", exc_info=True)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        cached = {m.id: m for m in payload.cached_messages}
        for message_id in payload.message_ids:
            if message_id in cached and cached[message_id].author.id not in self._onboarded:
                continue
            try:
                await staff_store.delete_message(message_id)
            except Exception:
                log.info("Staff vault delete failed", exc_info=True)

    # --- first-run learning ---

    @commands.Cog.listener()
    async def on_staff_onboarded(self, member: discord.Member) -> None:
        asyncio.create_task(self._learn(member))

    def _learn_sources(self, guild: discord.Guild) -> list:
        sources = [c for c in guild.text_channels if self._visible_to_members(c, guild)]
        sources += [th for th in guild.threads if self._visible_to_members(th, guild)]
        sources.sort(key=lambda c: getattr(c, "last_message_id", None) or 0, reverse=True)
        return sources[:LEARN_CHANNELS]

    async def _still_onboarded(self, user_id: int) -> bool:
        return user_id in self._onboarded and await staff_store.get_profile(user_id) is not None

    async def _learn(self, member: discord.Member) -> None:
        user_id = member.id
        try:
            guild = self.bot.get_guild(self.settings.panel_guild_id)
            if guild is None:
                return
            stored = scanned = 0
            for channel in self._learn_sources(guild):
                if stored >= LEARN_MESSAGES or not await self._still_onboarded(user_id):
                    break
                scanned += 1
                found = []
                try:
                    async for message in channel.history(limit=LEARN_PER_CHANNEL):
                        if message.author.id == user_id and (message.content or "").strip():
                            found.append(message)
                            if stored + len(found) >= LEARN_MESSAGES:
                                break
                except (discord.Forbidden, discord.HTTPException):
                    continue
                if not await self._still_onboarded(user_id):
                    break
                for message in found:
                    await staff_store.store_message(
                        message.id, user_id, guild.id, channel.id, message.content, message.created_at
                    )
                    stored += 1
                await asyncio.sleep(0.5)
            if not await self._still_onboarded(user_id):
                return
            await staff_store.mark_learned(user_id)
            await staff_assistant.distill(self.bot, user_id)
            log.info("Staff Bulma learned %s messages from %s channels for %s", stored, scanned, user_id)
        except Exception:
            log.exception("Staff Bulma first-run learning failed", extra={"user_id": user_id})

    # --- DMs and pings in staff-only channels ---

    async def handles(self, message: discord.Message) -> bool:
        """True when Staff Bulma answers this message instead of the support AI: an onboarded staff member's DM, or
        their ping in a main-guild channel members can't see (never a ticket)."""
        author = message.author
        if author.bot or getattr(message, "webhook_id", None) or author.id not in self._onboarded:
            return False
        channel = message.channel
        if isinstance(channel, discord.DMChannel):
            text = (message.content or "").strip()
            if not text or text.startswith(COMMAND_PREFIXES):
                return False
            guild_id = None
        else:
            guild = message.guild
            if guild is None or guild.id != self.settings.panel_guild_id:
                return False
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return False
            if self._visible_to_members(channel, guild) or self._member_role(guild) is None:
                return False
            base = channel.parent if isinstance(channel, discord.Thread) else channel
            if base is None or self._is_ticket(base):
                return False
            bot_user = self.bot.user
            if not (_is_pinging_bot(message, bot_user) and _has_support_request_content(message, bot_user)):
                return False
            guild_id = guild.id
        if await staff_store.get_profile(author.id) is None:
            return False
        return await self._gate(author, guild_id) is not None

    async def _history(self, message: discord.Message) -> list[dict]:
        bot_id = getattr(self.bot.user, "id", None)
        turns = []
        async for old in message.channel.history(limit=HISTORY_MESSAGES * 3, before=message):
            if old.author.id == bot_id:
                content, role = _card_text(old) or old.content, "assistant"
            elif old.author.id == message.author.id:
                content, role = _strip_bot_mentions(old.content or "", self.bot.user), "user"
            else:
                continue
            if content and content.strip():
                turns.append({"role": role, "content": content.strip()})
            if len(turns) >= HISTORY_MESSAGES:
                break
        turns.reverse()
        return turns

    async def _answer_message(self, message: discord.Message) -> None:
        member = await self._gate(message.author, getattr(message.guild, "id", None))
        if member is None:
            return
        channel = message.channel
        text = _strip_bot_mentions(message.content or "", self.bot.user)
        lock = self._locks.setdefault(member.id, asyncio.Lock())
        async with lock:
            async with channel.typing():
                history = await self._history(message)
                reply, rows = await self.answer_text(member, text, history, origin="discord", channel_id=channel.id)
            await message.reply(view=ui.reply_view(reply), allowed_mentions=NO_MENTIONS, mention_author=False)
            for row in rows:
                sent = await channel.send(view=ui.confirm_view(row, reply.language), allowed_mentions=NO_MENTIONS)
                await staff_store.set_action_card(row["id"], channel.id, sent.id)

    # --- commands ---

    @bulma.command(name="ask", **described(DESC, "ask"))
    @discord.option("prompt", str, **described(DESC, "prompt"), max_length=1500)
    async def ask(self, ctx: discord.ApplicationContext, prompt: str):
        interaction = ctx.interaction
        member = await self._staff_member(interaction)
        if member is None:
            return await self._deny(interaction)
        language = await self._language(member)
        if await staff_store.get_profile(member.id) is None:
            return await interaction.response.send_message(
                view=ui.consent_card(language), ephemeral=True, allowed_mentions=NO_MENTIONS
            )
        await interaction.response.defer(ephemeral=True, invisible=False)
        reply, rows = await self.answer_text(member, prompt, [], origin="discord", channel_id=interaction.channel_id)
        await interaction.followup.send(view=ui.reply_view(reply), ephemeral=True, allowed_mentions=NO_MENTIONS)
        for row in rows:
            await interaction.followup.send(
                view=ui.confirm_view(row, reply.language), ephemeral=True, allowed_mentions=NO_MENTIONS
            )

    @bulma.command(name="settings", **described(DESC, "settings"))
    async def settings_command(self, ctx: discord.ApplicationContext):
        interaction = ctx.interaction
        member = await self._staff_member(interaction)
        if member is None:
            return await self._deny(interaction)
        await self._open_setup(interaction, member)

    async def _open_setup(self, interaction: discord.Interaction, member: discord.Member) -> None:
        language = await self._language(member)
        profile = await staff_store.get_profile(member.id)
        if profile is None:
            return await interaction.response.send_message(
                view=ui.consent_card(language), ephemeral=True, allowed_mentions=NO_MENTIONS
            )
        await interaction.response.send_modal(
            ui.setup_modal(
                ui.EDIT,
                language,
                nickname=profile["nickname"],
                stored_language=profile.get("language"),
                timezone=profile.get("timezone") or "UTC",
                ticket_info=bool(profile.get("ticket_info")),
            )
        )

    @bulma.command(name="forget", **described(DESC, "forget"))
    async def forget(self, ctx: discord.ApplicationContext):
        interaction = ctx.interaction
        member = await self._staff_member(interaction)
        if member is None:
            return await self._deny(interaction)
        language = await self._language(member)

        async def confirmed(click: discord.Interaction) -> None:
            await staff_store.forget(member.id)
            self._onboarded.discard(member.id)
            await click.response.edit_message(view=card(t(ui.TEXT, "forgot", language), color=ui.GREY))

        await interaction.response.send_message(view=ui.ForgetView(language, confirmed), ephemeral=True)

    @bulma.command(name="export", **described(DESC, "export"))
    async def export(self, ctx: discord.ApplicationContext):
        interaction = ctx.interaction
        member = await self._staff_member(interaction)
        if member is None:
            return await self._deny(interaction)
        language = await self._language(member)
        payload = export_payload(
            await staff_store.get_profile(member.id),
            await staff_store.list_notes(member.id),
            await staff_store.list_reminders(member.id),
            await staff_store.export_vault(member.id),
        )
        await interaction.response.send_message(
            t(ui.TEXT, "export", language), file=discord.File(io.BytesIO(payload), filename="bulma-export.json"), ephemeral=True
        )

    @bulma.command(name="connect", **described(DESC, "connect"))
    async def connect(self, ctx: discord.ApplicationContext):
        interaction = ctx.interaction
        member = await self._staff_member(interaction)
        if member is None:
            return await self._deny(interaction)
        language = await self._language(member)
        if await staff_store.get_profile(member.id) is None:
            return await interaction.response.send_message(
                view=ui.consent_card(language), ephemeral=True, allowed_mentions=NO_MENTIONS
            )
        token = await staff_store.issue_token(member.id)
        url = self.settings.panel_public_url.rstrip("/") + "/mcp"
        command = f'claude mcp add --transport http bulma {url} --header "Authorization: Bearer {token}"'
        await interaction.response.send_message(
            view=card(
                t(ui.TEXT, "connect_title", language),
                t(ui.TEXT, "connect_body", language, command=command, url=url),
                color=ui.ACTION_COLOR,
            ),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    # --- interactions ---

    @discord.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id") or ""
        if interaction.type == discord.InteractionType.modal_submit:
            if (mode := ui.parse_form_id(custom_id)) is not None:
                await self._on_form_submit(interaction, mode)
            return
        if interaction.type != discord.InteractionType.component:
            return
        if custom_id == ui.SETUP_ID:
            member = await self._staff_member(interaction)
            if member is None:
                return await self._deny(interaction)
            language = await self._language(member)
            if await staff_store.get_profile(member.id) is not None:
                return await self._open_setup(interaction, member)
            return await interaction.response.send_modal(
                ui.setup_modal(ui.NEW, language, nickname=member.display_name)
            )
        if (parsed := ui.parse_custom_id(custom_id)) is not None:
            await self._on_action_button(interaction, *parsed)

    async def _on_form_submit(self, interaction: discord.Interaction, mode: str) -> None:
        member = await self._staff_member(interaction)
        if member is None:
            return await self._deny(interaction)
        language = await self._language(member)

        async def refuse(key: str) -> None:
            await interaction.response.send_message(t(ui.TEXT, key, language), ephemeral=True)

        form = ui.read_form((interaction.data or {}).get("components"))
        if not form["nickname"]:
            return await refuse("bad_nick")
        zone, error = ui.parse_timezone(form["timezone"])
        if zone is None:
            return await refuse(error or "bad_tz")
        existing = await staff_store.get_profile(member.id)
        is_new = existing is None
        if is_new and not form["consent"]:
            return await refuse("no_consent")
        chosen = None if form["language"] == ui.AUTO else form["language"]
        await staff_store.save_profile(
            member.id, nickname=form["nickname"], language=chosen, timezone=zone, ticket_info=form["ticket_info"]
        )
        language = staff_assistant.language_for(member, {"language": chosen}, self.settings)
        key = "done" if is_new else "saved"
        self._onboarded.add(member.id)
        await interaction.response.send_message(
            t(ui.TEXT, key, language, nickname=form["nickname"]), ephemeral=True, allowed_mentions=NO_MENTIONS
        )
        if is_new:
            self.bot.dispatch("staff_onboarded", member)

    async def _show_outcome(self, interaction: discord.Interaction, row: dict | None, language: str) -> None:
        if row is not None and row["outcome"] == "running":
            return await interaction.followup.send(already_working(language), ephemeral=True)
        if row is not None:
            await interaction.edit_original_response(view=ui.outcome_card(row, language))
        await interaction.followup.send(ui.outcome_text(row, language), ephemeral=True)

    async def _on_action_button(self, interaction: discord.Interaction, action: str, action_id: int) -> None:
        row = await staff_store.get_action(action_id)
        if row is not None and row["user_id"] != interaction.user.id:
            return await interaction.response.send_message(
                t(ui.TEXT, "not_yours", reader_language(interaction)), ephemeral=True
            )
        member = await self._staff_member(interaction)
        if member is None:
            return await self._deny(interaction)
        language = await self._language(member)
        if row is None:
            return await interaction.response.send_message(t(ui.TEXT, "already_expired", language), ephemeral=True)
        if action == ui.CANCEL:
            if await staff_store.cancel_action(action_id, member.id):
                return await interaction.response.edit_message(view=ui.cancelled_card(row["summary"], language))
            await interaction.response.defer(invisible=True)
            return await self._show_outcome(interaction, await staff_store.get_action(action_id), language)

        async def run() -> bool:
            claimed = await staff_store.claim_action(action_id, member.id)
            if claimed is None:
                await self._show_outcome(interaction, await staff_store.get_action(action_id), language)
                return True
            try:
                result = await staff_tools.execute_action(claimed, self.bot, member)
            except (ModActionError, ValueError) as error:
                text = error.text(language) if isinstance(error, ModActionError) else str(error)
                await staff_store.finish_action(action_id, "failed", text)
                await interaction.edit_original_response(view=ui.failed_card(claimed["summary"], text))
                return True
            except Exception:
                await staff_store.release_action(action_id)
                raise
            await staff_store.finish_action(action_id, "done", result)
            await interaction.edit_original_response(view=ui.done_card(claimed["summary"], result))
            return True

        await while_busy(interaction, self._working, run)

    # --- sweeps ---

    @tasks.loop(minutes=1)
    async def sweep_actions(self) -> None:
        try:
            rows = await staff_store.expire_actions(datetime.now(timezone.utc))
        except Exception:
            log.exception("Staff action expiry sweep failed")
            return
        for row in rows:
            if not (row.get("channel_id") and row.get("message_id")):
                continue
            try:
                language = staff_assistant.language_for(
                    None, await staff_store.get_profile(row["user_id"]), self.settings
                )
                channel = self.bot.get_channel(row["channel_id"]) or await self.bot.fetch_channel(row["channel_id"])
                message = channel.get_partial_message(row["message_id"])
                await message.edit(view=ui.expired_card(row["summary"], language))
            except (discord.NotFound, discord.Forbidden):
                continue
            except Exception:
                log.info("Couldn't collapse an expired staff card", exc_info=True)

    @tasks.loop(minutes=1)
    async def send_reminders(self) -> None:
        try:
            due = await staff_store.due_reminders(datetime.now(timezone.utc))
        except Exception:
            log.exception("Staff reminder sweep failed")
            return
        for reminder in due:
            try:
                if not await staff_store.claim_reminder(reminder["id"]):
                    continue
                profile = await staff_store.get_profile(reminder["user_id"])
                language = staff_assistant.language_for(self.bot.get_user(reminder["user_id"]), profile, self.settings)
                user = self.bot.get_user(reminder["user_id"]) or await self.bot.fetch_user(reminder["user_id"])
                await user.send(t(ui.TEXT, "reminder", language, text=reminder["text"]), allowed_mentions=NO_MENTIONS)
            except Exception:
                log.info("Couldn't deliver a staff reminder", exc_info=True)

    @tasks.loop(hours=1)
    async def distill_all(self) -> None:
        if ai_budget.is_paused(self.settings):
            return
        try:
            profiles = await staff_store.list_profiles()
        except Exception:
            log.exception("Staff profile listing failed")
            return
        for profile in profiles:
            try:
                since = profile.get("distilled_at") or profile["consent_at"]
                if await staff_store.count_since(profile["user_id"], since) >= self.settings.staff_ai_distill_every:
                    await staff_assistant.distill(self.bot, profile["user_id"])
            except Exception:
                log.exception("Staff memory distill failed", extra={"user_id": profile.get("user_id")})

    @sweep_actions.before_loop
    @send_reminders.before_loop
    @distill_all.before_loop
    async def _before_loops(self) -> None:
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot) -> None:
    bot.add_cog(StaffAssistantCog(bot))
