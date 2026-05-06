import logging
import time

import discord
from discord.ext import commands

from bulmaai.config import Settings
from bulmaai.services.moderation import (
    AttachmentInfo,
    MessageSignal,
    ModerationAction,
    ModerationConfig,
    ModerationDecision,
    ModerationState,
    evaluate_message,
)
from bulmaai.utils.permissions import is_admin, is_staff


log = logging.getLogger(__name__)


class ModerationCog(commands.Cog):
    """MVP anti-spam and harmful-link guardrail."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._state = ModerationState()

    def _settings(self) -> Settings:
        return self.bot.settings

    def _decision_config(self) -> ModerationConfig:
        settings = self._settings()
        return ModerationConfig(
            blocked_domains=tuple(settings.moderation_blocked_domains),
            allowed_domains=tuple(settings.moderation_allowed_domains),
            block_discord_invites=settings.moderation_block_discord_invites,
            image_burst_count=settings.moderation_image_burst_count,
            image_burst_window_seconds=settings.moderation_image_burst_window_seconds,
            link_burst_count=settings.moderation_link_burst_count,
            link_burst_window_seconds=settings.moderation_link_burst_window_seconds,
        )

    def _is_exempt(self, member: discord.Member, channel_id: int) -> bool:
        settings = self._settings()
        if channel_id in set(settings.moderation_excluded_channel_ids):
            return True
        if is_admin(member) or is_staff(member, settings=settings):
            return True
        exempt_roles = {int(role_id) for role_id in settings.moderation_exempt_role_ids}
        return any(role.id in exempt_roles for role in getattr(member, "roles", []))

    @staticmethod
    def _message_signal(message: discord.Message) -> MessageSignal | None:
        if not message.guild or not isinstance(message.author, discord.Member):
            return None
        return MessageSignal(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            content=message.content or "",
            attachments=tuple(
                AttachmentInfo(
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    url=attachment.url,
                    size=attachment.size,
                    width=getattr(attachment, "width", None),
                    height=getattr(attachment, "height", None),
                )
                for attachment in message.attachments
            ),
        )

    async def _resolve_log_channel(self) -> discord.abc.Messageable | None:
        settings = self._settings()
        channel_id = settings.moderation_log_channel_id or settings.discord_log_channel_id
        if channel_id is None:
            return None

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                log.exception(
                    "Failed to fetch moderation log channel",
                    extra={"event": "moderation_log_channel_fetch_failed", "channel_id": channel_id},
                )
                return None

        return channel if hasattr(channel, "send") else None

    def _build_log_embed(
        self,
        message: discord.Message,
        decision: ModerationDecision,
        *,
        deleted: bool,
    ) -> discord.Embed:
        color = discord.Color.red() if decision.action is ModerationAction.DELETE else discord.Color.orange()
        embed = discord.Embed(
            title="Moderation Alert",
            description=decision.details or decision.reason,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Action", value=decision.action.value, inline=True)
        embed.add_field(name="Reason", value=decision.reason, inline=True)
        embed.add_field(name="Deleted", value=str(deleted), inline=True)
        embed.add_field(name="User", value=f"{message.author} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Channel", value=f"<#{message.channel.id}> (`{message.channel.id}`)", inline=False)
        embed.add_field(name="Message", value=f"[Jump to message]({message.jump_url})", inline=False)
        if decision.defanged_domains:
            embed.add_field(
                name="Domains",
                value=", ".join(f"`{domain}`" for domain in decision.defanged_domains[:10]),
                inline=False,
            )
        if decision.invites:
            codes = ", ".join(f"`{invite.domain}/{invite.code}`" for invite in decision.invites[:5])
            embed.add_field(name="Invites", value=codes, inline=False)
        if decision.image_count:
            embed.add_field(name="Image Count", value=str(decision.image_count), inline=True)
        if message.attachments:
            filenames = ", ".join(f"`{attachment.filename}`" for attachment in message.attachments[:8])
            embed.add_field(name="Attachments", value=filenames, inline=False)
        return embed

    async def _send_log(
        self,
        message: discord.Message,
        decision: ModerationDecision,
        *,
        deleted: bool,
    ) -> None:
        channel = await self._resolve_log_channel()
        if channel is None:
            return
        try:
            await channel.send(
                embed=self._build_log_embed(message, decision, deleted=deleted),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception(
                "Failed to send moderation log",
                extra={
                    "event": "moderation_log_send_failed",
                    "channel_id": getattr(channel, "id", None),
                    "message_id": message.id,
                    "user_id": message.author.id,
                },
            )

    async def _apply_decision(self, message: discord.Message, decision: ModerationDecision) -> None:
        if decision.action is ModerationAction.ALLOW:
            return

        deleted = False
        if decision.action is ModerationAction.DELETE:
            try:
                await message.delete(reason=f"BulmaAI moderation: {decision.reason}")
                deleted = True
            except discord.Forbidden:
                log.warning(
                    "Missing permission to delete suspicious message",
                    extra={
                        "event": "moderation_delete_forbidden",
                        "guild_id": getattr(message.guild, "id", None),
                        "channel_id": message.channel.id,
                        "message_id": message.id,
                        "user_id": message.author.id,
                    },
                )
            except discord.HTTPException:
                log.exception(
                    "Failed to delete suspicious message",
                    extra={
                        "event": "moderation_delete_failed",
                        "guild_id": getattr(message.guild, "id", None),
                        "channel_id": message.channel.id,
                        "message_id": message.id,
                        "user_id": message.author.id,
                    },
                )

        await self._send_log(message, decision, deleted=deleted)

    async def _inspect_message(self, message: discord.Message) -> None:
        settings = self._settings()
        if not settings.moderation_enabled or message.author.bot:
            return
        if not message.guild or not isinstance(message.author, discord.Member):
            return
        if self._is_exempt(message.author, message.channel.id):
            return

        signal = self._message_signal(message)
        if signal is None:
            return

        decision = evaluate_message(
            signal,
            self._decision_config(),
            self._state,
            now=time.monotonic(),
        )
        await self._apply_decision(message, decision)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._inspect_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content and before.attachments == after.attachments:
            return
        await self._inspect_message(after)


def setup(bot: discord.Bot):
    bot.add_cog(ModerationCog(bot))
