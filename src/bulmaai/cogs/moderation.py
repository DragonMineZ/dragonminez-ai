import asyncio
import io
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from bulmaai.config import Settings
from bulmaai.services import automod_hits, mod_actions, mod_cases, scam_images
from bulmaai.services.mod_actions import ModActionError
from bulmaai.services.phishdestroy import PhishDestroyClient, PhishDestroyUnavailable, PhishDestroyVerdict
from bulmaai.services.moderation import (
    AttachmentInfo,
    DomainClassification,
    MessageSignal,
    ModerationAction,
    ModerationConfig,
    ModerationDecision,
    ModerationState,
    classify_domain,
    defang_domain,
    ImagePost,
    confirm_image_burst,
    evaluate_message,
    extract_image_attachments,
    extract_urls,
    apply_rule_action,
    exempt_filters,
    image_signature,
    parse_filter_rules,
    without_filters,
)
from bulmaai.ui.mod_views import quick_actions_view
from bulmaai.utils.permissions import is_admin, is_staff


log = logging.getLogger(__name__)

# ponytail: in-memory incidents; a restart mid-raid just opens a fresh one.
INCIDENT_TTL_SECONDS = 300
REPEAT_OFFENSE_TIMEOUT_HITS = 3
LOG_UPDATE_DEBOUNCE_SECONDS = 5
# ponytail: fixed settle delay; move to Settings if mods want to tune it.
IMAGE_BURST_CONFIRM_SECONDS = 8
IMAGE_BURST_REASON = "image burst"
# ponytail: fixed 10-minute timeout for a human escalated by repeated content-filter hits;
# spam-bot-shaped incidents (image/link burst, cross-channel duplicate_spam) keep the long one.
HUMAN_ESCALATION_TIMEOUT_SECONDS = 600
_HUMAN_SHAPED_REASONS = frozenset(
    {"excessive_caps", "excessive_emoji", "wall_of_text", "zalgo", "fast_messages", "duplicate_spam"}
)
_SEVERITY = {
    ModerationAction.ALLOW: 0,
    ModerationAction.ALERT: 1,
    ModerationAction.DELETE: 2,
    ModerationAction.TIMEOUT: 3,
}
# One warn strike per incident, on the first hit, for reasons that are clearly one person's fault
# (not a shared/compromised-account spam shape).
# everyone_ping is delete + notice only: new members type "@everyone help" innocently.
_WARN_REASONS = frozenset({"banned_word", "mass_mention", "blocked_domain", "discord_invite"})
_READABLE_REASONS = {
    "banned_word": "a banned word",
    "mass_mention": "mass mentions",
    "everyone_ping": "an @everyone/@here ping",
    "blocked_domain": "a blocked link",
    "discord_invite": "a Discord invite link",
}
_NOTICE_TEXT = {
    "excessive_caps": "please avoid excessive caps.",
    "excessive_emoji": "please use fewer emoji.",
    "wall_of_text": "please avoid walls of text.",
    "zalgo": "please avoid glitch/zalgo text.",
    "fast_messages": "please slow down.",
    "banned_word": "that word isn't allowed here.",
    "mass_mention": "please avoid mass-mentioning members.",
    "everyone_ping": "you can't ping @everyone/@here here.",
}


def scam_check_applies(decision: ModerationDecision, *, enforcing: bool) -> bool:
    """A known scam image outranks an ALLOW and, once enforcing, shouldn't wait out the image-burst settle
    delay. In shadow mode a match only alerts, so it must never replace a burst's timeout."""
    return decision.action is ModerationAction.ALLOW or (enforcing and decision.reason == IMAGE_BURST_REASON)


@dataclass
class _Incident:
    """One alert per offender per wave; follow-ups are enforced quietly and folded in."""

    decision: ModerationDecision
    first_message: discord.Message
    hits: int = 0
    delete_hits: int = 0
    revision: int = 0
    deleted: int = 0
    purged: int = 0
    last_hit: float = 0.0
    channel_ids: set[int] = field(default_factory=set)
    timeout_attempted: bool = False
    timeout_status: str = ""
    timed_out: bool = False
    notice_sent: bool = False
    log_message: discord.Message | None = None
    update_task: asyncio.Task | None = None
    hit_id: int | None = None
    # Images taught to "Delete & learn" so far; merged into on every update_hit(image_hashes=...) call.
    image_hashes: tuple[int, ...] = field(default_factory=tuple)


class ModerationCog(commands.Cog):
    """MVP anti-spam and harmful-link guardrail."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._state = ModerationState()
        # (guild_id, author_id) -> {channel_id: last_message_monotonic} so a
        # burst purge can clean every channel the spammer recently posted in.
        self._recent_message_channels: dict[tuple[int, int], dict[int, float]] = {}
        self._incidents: dict[tuple[int, int], _Incident] = {}
        self._recent_images: dict[tuple[int, int], dict[int, ImagePost]] = {}
        self._pending_image_bursts: dict[tuple[int, int], list[discord.Message]] = {}
        self._scam_images_loaded = False
        settings = self._settings()
        self._phishdestroy: PhishDestroyClient | None = None
        self._phishdestroy_down = False
        if settings.phishdestroy_enabled:
            self._phishdestroy = PhishDestroyClient(
                base_url=settings.phishdestroy_api_base_url,
                timeout_seconds=settings.phishdestroy_timeout_seconds,
                safe_ttl_seconds=settings.phishdestroy_safe_ttl_seconds,
                threat_ttl_seconds=settings.phishdestroy_threat_ttl_seconds,
            )

    def _settings(self) -> Settings:
        return self.bot.settings

    def _filters_off(self, message: discord.Message | None = None) -> frozenset[str]:
        """Filters switched off in the panel, plus those this message's channel or author is exempt from."""
        settings = self._settings()
        off = set(settings.moderation_disabled_filters)
        if message is not None:
            channel = message.channel
            places = {channel.id, getattr(channel, "parent_id", None), getattr(channel, "category_id", None)}
            roles = {role.id for role in getattr(message.author, "roles", [])}
            off.update(exempt_filters(parse_filter_rules(settings.moderation_filter_rules), places, roles))
        return frozenset(off)

    def _decision_config(self, off: frozenset[str] | None = None) -> ModerationConfig:
        settings = self._settings()
        config = ModerationConfig(
            blocked_domains=tuple(settings.moderation_blocked_domains),
            allowed_domains=tuple(settings.moderation_allowed_domains),
            block_discord_invites=settings.moderation_block_discord_invites,
            image_burst_count=settings.moderation_image_burst_count,
            image_burst_window_seconds=settings.moderation_image_burst_window_seconds,
            image_burst_min_messages=settings.moderation_image_burst_min_messages,
            link_burst_count=settings.moderation_link_burst_count,
            link_burst_window_seconds=settings.moderation_link_burst_window_seconds,
            banned_words=tuple(settings.moderation_banned_words),
            mass_mention_limit=settings.moderation_mass_mention_limit,
            block_everyone_ping=settings.moderation_block_everyone_ping,
            duplicate_count=settings.moderation_duplicate_count,
            duplicate_window_seconds=settings.moderation_duplicate_window_seconds,
            fast_message_count=settings.moderation_fast_message_count,
            fast_message_window_seconds=settings.moderation_fast_message_window_seconds,
            caps_percent=settings.moderation_caps_percent,
            caps_min_length=settings.moderation_caps_min_length,
            emoji_limit=settings.moderation_emoji_limit,
            newline_limit=settings.moderation_newline_limit,
            zalgo_enabled=settings.moderation_zalgo_enabled,
        )
        return without_filters(config, sorted(self._filters_off() if off is None else off))

    def _phishdestroy_action(self) -> ModerationAction:
        value = self._settings().phishdestroy_action.lower().strip()
        if value == ModerationAction.DELETE.value:
            return ModerationAction.DELETE
        return ModerationAction.ALERT

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
            mention_count=len(set(message.raw_mentions)) + len(set(message.raw_role_mentions)),
            can_mention_everyone=bool(
                getattr(message.author.guild_permissions, "mention_everyone", False)
            ),
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

    def _build_log_embed(self, message: discord.Message, incident: "_Incident") -> discord.Embed:
        decision = incident.decision
        color = (
            discord.Color.red()
            if decision.action in (ModerationAction.DELETE, ModerationAction.TIMEOUT)
            else discord.Color.orange()
        )
        embed = discord.Embed(
            title="Moderation Alert",
            description=decision.details or decision.reason,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Action", value=decision.action.value, inline=True)
        embed.add_field(name="Reason", value=decision.reason, inline=True)
        embed.add_field(name="Deleted", value=f"{incident.deleted}/{incident.hits}", inline=True)
        if incident.timeout_status:
            embed.add_field(name="Timeout", value=incident.timeout_status, inline=True)
            embed.add_field(name="Messages Purged", value=str(incident.purged), inline=True)
        if decision.source:
            embed.add_field(name="Source", value=decision.source, inline=True)
        embed.add_field(name="User", value=f"{message.author} (`{message.author.id}`)", inline=False)
        channels = " ".join(f"<#{channel_id}>" for channel_id in list(incident.channel_ids)[:10])
        embed.add_field(name=f"Channels ({len(incident.channel_ids)})", value=channels, inline=False)
        embed.add_field(name="First Message", value=f"[Jump to message]({message.jump_url})", inline=False)
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
        handled = self._existing_handled_field(incident)
        if handled is not None:
            embed.add_field(name=handled.name, value=handled.value, inline=handled.inline)
        image_url = self._existing_image_url(incident)
        if image_url is not None:
            embed.set_image(url=image_url)
        return embed

    @staticmethod
    def _existing_handled_field(incident: "_Incident") -> "discord.EmbedField | None":
        """cogs/mod_interactions.py adds a "Handled" field when a mod clicks Timeout/Ban/Dismiss on
        the alert. We re-render the whole embed on every debounced update, which would wipe that
        field, so carry it forward from whatever py-cord has cached on the Message object.
        Best-effort on purpose: no re-fetch, so a very stale cache can miss it."""
        message = incident.log_message
        if message is None or not message.embeds:
            return None
        for embed_field in message.embeds[0].fields:
            if embed_field.name == "Handled":
                return embed_field
        return None

    @staticmethod
    def _existing_image_url(incident: "_Incident") -> str | None:
        """Same idea as _existing_handled_field: the debounced re-render must not drop the
        re-uploaded preview image, so carry its resolved URL forward instead of re-uploading
        the file (or passing files=/attachments=) on every edit."""
        message = incident.log_message
        if message is None or not message.embeds:
            return None
        image = message.embeds[0].image
        return image.url if image is not None else None

    @staticmethod
    def _first_image_attachment(message: discord.Message) -> "discord.Attachment | None":
        images = extract_image_attachments(message.attachments)
        if not images:
            return None
        first_url = images[0].url
        return next((attachment for attachment in message.attachments if attachment.url == first_url), None)

    async def _build_alert_file(self, message: discord.Message) -> tuple[discord.File, str] | None:
        """A reviewable copy of the first flagged image, re-uploaded since the original message
        (and its attachment URL) is often purged before staff look at the alert."""
        attachment = self._first_image_attachment(message)
        if attachment is None:
            return None
        try:
            data = await scam_images.fetch_preview(attachment)
        except Exception:
            log.exception(
                "Failed to fetch an image preview for a moderation alert",
                extra={"event": "moderation_image_preview_failed", "message_id": message.id},
            )
            return None
        if data is None:
            return None
        filename = attachment.filename or "preview.png"
        return discord.File(io.BytesIO(data), filename=filename), filename

    @staticmethod
    def _quick_actions_view_for(incident: "_Incident") -> discord.ui.View:
        message = incident.first_message
        if ModerationCog._first_image_attachment(message) is not None:
            # Image alerts get "Delete & learn" instead of a plain delete; message=(...) lets it
            # remove the flagged message the way the plain "delete" action does.
            actions = (
                ("untimeout", "ban", "learn", "falsepos")
                if incident.timed_out
                else ("timeout", "ban", "learn", "falsepos")
            )
            return quick_actions_view(message.author.id, actions=actions, message=(message.channel.id, message.id))
        actions = ("untimeout", "ban", "falsepos") if incident.timed_out else ("timeout", "ban", "falsepos")
        return quick_actions_view(message.author.id, actions=actions)

    async def _send_log(self, incident: "_Incident") -> None:
        channel = await self._resolve_log_channel()
        if channel is None:
            return
        message = incident.first_message
        embed = self._build_log_embed(message, incident)
        send_kwargs: dict[str, object] = {
            "embed": embed,
            "view": self._quick_actions_view_for(incident),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        preview = await self._build_alert_file(message)
        if preview is not None:
            file, filename = preview
            embed.set_image(url=f"attachment://{filename}")
            send_kwargs["file"] = file
        try:
            incident.log_message = await channel.send(**send_kwargs)
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
            return
        if incident.hit_id is not None:
            await self._update_hit_best_effort(incident.hit_id, alert_message_id=incident.log_message.id)

    async def _record_hit(self, message: discord.Message, incident: "_Incident") -> None:
        """Best-effort: automod keeps working even if this row never gets written."""
        decision = incident.decision
        try:
            incident.hit_id = await automod_hits.record_hit(
                guild_id=message.guild.id,
                user_id=message.author.id,
                reason=decision.reason,
                action=decision.action.value,
                details=decision.details or None,
                domains=decision.domains,
                scam_hash_id=decision.scam_hash_id,
            )
        except Exception:
            log.exception(
                "Failed to record automod hit",
                extra={"event": "automod_hit_record_failed", "user_id": message.author.id, "reason": decision.reason},
            )
            incident.hit_id = None

    async def _update_hit_best_effort(self, hit_id: int, **fields: object) -> None:
        try:
            await automod_hits.update_hit(hit_id, **fields)
        except Exception:
            log.exception(
                "Failed to update automod hit",
                extra={"event": "automod_hit_update_failed", "hit_id": hit_id, "fields": sorted(fields)},
            )

    async def _add_image_hashes(self, incident: "_Incident", hashes: tuple[int, ...]) -> None:
        """Merges into whatever's already stored so multiple hits on one incident don't clobber
        each other's images; skipped once the row failed to record in the first place."""
        if incident.hit_id is None or not hashes:
            return
        merged = tuple(dict.fromkeys((*incident.image_hashes, *hashes)))
        if merged == incident.image_hashes:
            return
        incident.image_hashes = merged
        await self._update_hit_best_effort(incident.hit_id, image_hashes=merged)

    def _schedule_log_update(self, incident: "_Incident") -> None:
        incident.revision += 1
        if incident.update_task is None or incident.update_task.done():
            incident.update_task = asyncio.create_task(self._flush_log_update(incident))

    async def _flush_log_update(self, incident: "_Incident") -> None:
        # Debounced: a 20-message spam wave becomes a few edits, not 20 alerts.
        # Loops so a hit that lands mid-edit is not lost.
        synced = 0
        while synced != incident.revision:
            await asyncio.sleep(LOG_UPDATE_DEBOUNCE_SECONDS)
            if incident.log_message is None:
                return
            synced = incident.revision
            try:
                edit_kwargs = {"embed": self._build_log_embed(incident.first_message, incident)}
                # Once a mod has clicked a button, leave whatever view is on the message alone
                # (omitting `view` keeps it; passing a fresh one would replace it).
                if self._existing_handled_field(incident) is None:
                    edit_kwargs["view"] = self._quick_actions_view_for(incident)
                await incident.log_message.edit(**edit_kwargs)
            except discord.HTTPException:
                log.debug("Failed to update moderation incident log", exc_info=True)
                return

    def _open_incident(self, message: discord.Message, decision: ModerationDecision) -> tuple["_Incident", bool]:
        """Synchronous on purpose: runs before any await so concurrent on_message
        handlers for the same spammer all land in one incident."""
        now = time.monotonic()
        self._incidents = {
            key: incident
            for key, incident in self._incidents.items()
            if now - incident.last_hit <= INCIDENT_TTL_SECONDS
        }
        key = (message.guild.id, message.author.id)
        incident = self._incidents.get(key)
        is_new = incident is None
        if incident is None:
            incident = _Incident(decision=decision, first_message=message)
            self._incidents[key] = incident
        incident.last_hit = now
        incident.hits += 1
        incident.channel_ids.add(message.channel.id)
        if decision.action is ModerationAction.DELETE:
            incident.delete_hits += 1
        if (
            decision.action is ModerationAction.DELETE
            and incident.delete_hits >= REPEAT_OFFENSE_TIMEOUT_HITS
        ):
            # Repeated human-shaped hits (caps, emoji, etc.) get a short timeout; anything else
            # keeps the long spam-bot one (set on the ModerationDecision itself, or the default).
            timeout_seconds = (
                HUMAN_ESCALATION_TIMEOUT_SECONDS if decision.reason in _HUMAN_SHAPED_REASONS else decision.timeout_seconds
            )
            decision = replace(
                decision,
                action=ModerationAction.TIMEOUT,
                details=f"repeated {decision.reason}",
                timeout_seconds=timeout_seconds,
            )
        if _SEVERITY[decision.action] > _SEVERITY[incident.decision.action]:
            incident.decision = decision
        return incident, is_new

    async def _apply_decision(self, message: discord.Message, decision: ModerationDecision) -> bool:
        """Returns whether the author was timed out by this call."""
        if decision.action is ModerationAction.ALLOW:
            return False

        incident, is_new = self._open_incident(message, decision)
        # is_new/run_timeout must be decided and timeout_attempted flipped before the first await
        # below, in the same synchronous stretch as _open_incident: concurrent burst messages all
        # reach this point, and yielding any earlier would let more than one see run_timeout=True.
        run_timeout = incident.decision.action is ModerationAction.TIMEOUT and not incident.timeout_attempted
        if run_timeout:
            incident.timeout_attempted = True
        if is_new:
            await self._record_hit(message, incident)

        if incident.decision.action in (ModerationAction.DELETE, ModerationAction.TIMEOUT):
            if await self._delete_message(message, incident.decision):
                incident.deleted += 1
            if (
                incident.decision.action is ModerationAction.DELETE
                and incident.decision.reason in _NOTICE_TEXT
                and not incident.notice_sent
            ):
                incident.notice_sent = True
                await self._send_delete_notice(message, incident.decision)

        timed_out = False
        if run_timeout:
            timed_out, incident.timeout_status = await self._timeout_member(message, incident.decision)
            incident.timed_out = incident.timed_out or timed_out
            if incident.decision.purge:
                incident.purged += await self._purge_recent_messages(message, incident.decision)
            if timed_out and incident.hit_id is not None:
                await self._update_hit_best_effort(incident.hit_id, timed_out=True, action="timeout")

        warn = incident.decision.warn
        if is_new and (incident.decision.reason in _WARN_REASONS if warn is None else warn):
            await self._issue_warn_strike(message, incident)

        if is_new:
            hits_logged = incident.hits
            await self._send_log(incident)
            if incident.hits != hits_logged:
                self._schedule_log_update(incident)
        else:
            self._schedule_log_update(incident)
        if is_new or run_timeout:
            await self._record_case(message, incident.decision, timed_out=timed_out)
        return timed_out

    async def _send_delete_notice(self, message: discord.Message, decision: ModerationDecision) -> None:
        text = _NOTICE_TEXT.get(decision.reason)
        if text is None:
            return
        try:
            await message.channel.send(
                f"{message.author.mention}, {text}",
                delete_after=6,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass

    async def _issue_warn_strike(self, message: discord.Message, incident: "_Incident") -> None:
        """One warn per incident for reasons that are squarely one person's fault; this DMs them,
        records the warn and runs the warn ladder (which may itself timeout/kick/ban and log a case)."""
        decision = incident.decision
        readable = _READABLE_REASONS.get(decision.reason, decision.reason.replace("_", " "))
        try:
            result = await mod_actions.perform(
                self.bot,
                message.guild,
                action="warn",
                target_id=message.author.id,
                moderator=None,
                reason=f"Automod: {readable}",
                source="automod",
                log_case=False,
            )
        except ModActionError as error:
            log.warning(
                "Automod warn strike skipped: %s",
                error,
                extra={"event": "moderation_warn_strike_skipped", "user_id": message.author.id},
            )
            return
        except Exception:
            log.exception(
                "Automod warn strike failed",
                extra={"event": "moderation_warn_strike_failed", "user_id": message.author.id},
            )
            return
        if incident.hit_id is None:
            return
        fields: dict[str, object] = {}
        if result.case_id is not None:
            fields["warn_case_id"] = result.case_id
        if result.escalation is not None and result.escalation.action == "timeout":
            # A false positive on the reason that earned the warn must also undo this escalation.
            incident.timed_out = True
            fields["timed_out"] = True
        if fields:
            await self._update_hit_best_effort(incident.hit_id, **fields)

    async def _delete_message(self, message: discord.Message, decision: ModerationDecision) -> bool:
        extra = {
            "guild_id": getattr(message.guild, "id", None),
            "channel_id": message.channel.id,
            "message_id": message.id,
            "user_id": message.author.id,
        }
        try:
            await message.delete(reason=f"BulmaAI moderation: {decision.reason}")
            return True
        except discord.NotFound:
            return True  # already gone: purge, Discord AutoMod or another bot got it first
        except discord.Forbidden:
            log.warning(
                "Missing permission to delete suspicious message",
                extra={"event": "moderation_delete_forbidden", **extra},
            )
        except discord.HTTPException:
            log.exception(
                "Failed to delete suspicious message",
                extra={"event": "moderation_delete_failed", **extra},
            )
        return False

    async def _record_case(self, message: discord.Message, decision: ModerationDecision, *, timed_out: bool) -> None:
        """Best-effort entry in the panel's case log; automod keeps working if the DB is down."""
        duration_seconds = None
        if timed_out:
            duration_seconds = decision.timeout_seconds or self._settings().moderation_image_burst_timeout_seconds
        try:
            await mod_cases.record_case(
                guild_id=message.guild.id,
                user_id=message.author.id,
                action=decision.action.value,
                reason=(decision.details or decision.reason)[:500],
                duration_seconds=duration_seconds,
                source="automod",
            )
        except Exception:
            log.exception(
                "Failed to record automod case",
                extra={"event": "moderation_case_record_failed", "user_id": message.author.id},
            )

    @staticmethod
    def _timeout_blocker(member: discord.Member) -> str | None:
        """Why Discord would reject the timeout, checked up front instead of eating a 403."""
        guild = getattr(member, "guild", None)
        me = getattr(guild, "me", None)
        if me is None:
            return None
        if not me.guild_permissions.moderate_members:
            return "bot lacks Moderate Members"
        if getattr(getattr(member, "guild_permissions", None), "administrator", False):
            return "member has Administrator"
        if member.id == guild.owner_id:
            return "member is the server owner"
        if member.top_role >= me.top_role:
            return "member's top role is not below the bot's"
        return None

    async def _timeout_member(self, message: discord.Message, decision: ModerationDecision) -> tuple[bool, str]:
        member = message.author
        timeout_for = getattr(member, "timeout_for", None)
        if timeout_for is None:
            return False, "skipped: not a member"
        extra = {"guild_id": getattr(message.guild, "id", None), "user_id": member.id}
        blocker = self._timeout_blocker(member)
        if blocker:
            log.warning(
                "Cannot timeout member: %s",
                blocker,
                extra={"event": "moderation_timeout_forbidden", **extra},
            )
            return False, f"skipped: {blocker}"
        seconds = decision.timeout_seconds or self._settings().moderation_image_burst_timeout_seconds
        try:
            await timeout_for(timedelta(seconds=seconds), reason=f"BulmaAI moderation: {decision.reason}")
            return True, f"{seconds // 86400}d" if seconds >= 86400 else f"{seconds // 60}m"
        except discord.Forbidden:
            log.warning(
                "Missing permission to timeout member",
                extra={"event": "moderation_timeout_forbidden", **extra},
            )
        except discord.HTTPException:
            log.exception(
                "Failed to timeout member",
                extra={"event": "moderation_timeout_failed", **extra},
            )
        return False, "failed"

    def _record_recent_channel(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        window_seconds = self._settings().moderation_image_burst_purge_seconds
        cutoff = time.monotonic() - window_seconds
        if len(self._recent_message_channels) > 2048:
            self._recent_message_channels = {
                key: channels
                for key, channels in self._recent_message_channels.items()
                if any(seen_at >= cutoff for seen_at in channels.values())
            }
        key = (message.guild.id, message.author.id)
        channels = {
            channel_id: seen_at
            for channel_id, seen_at in self._recent_message_channels.get(key, {}).items()
            if seen_at >= cutoff
        }
        channels[message.channel.id] = time.monotonic()
        self._recent_message_channels[key] = channels

    def _recent_channel_ids(self, guild_id: int, author_id: int, *, window_seconds: float) -> list[int]:
        channels = self._recent_message_channels.get((guild_id, author_id), {})
        cutoff = time.monotonic() - window_seconds
        return [channel_id for channel_id, seen_at in channels.items() if seen_at >= cutoff]

    async def _purge_recent_messages(self, message: discord.Message, decision: ModerationDecision) -> int:
        guild = message.guild
        if guild is None:
            return 0
        settings = self._settings()
        author_id = message.author.id
        purge_after = discord.utils.utcnow() - timedelta(
            seconds=settings.moderation_image_burst_purge_seconds
        )
        channel_ids = set(
            self._recent_channel_ids(
                guild.id,
                author_id,
                window_seconds=settings.moderation_image_burst_purge_seconds,
            )
        )
        channel_ids.add(message.channel.id)

        purged = 0
        for channel_id in channel_ids:
            channel = guild.get_channel(channel_id)
            if channel is None or not hasattr(channel, "purge"):
                continue
            try:
                removed = await channel.purge(
                    limit=200,
                    after=purge_after,
                    check=lambda candidate: candidate.author.id == author_id,
                    reason=f"BulmaAI moderation: {decision.reason}",
                )
                purged += len(removed)
            except discord.Forbidden:
                log.warning(
                    "Missing permission to purge burst messages",
                    extra={
                        "event": "moderation_purge_forbidden",
                        "guild_id": guild.id,
                        "channel_id": channel_id,
                        "user_id": author_id,
                    },
                )
            except discord.HTTPException:
                log.exception(
                    "Failed to purge burst messages",
                    extra={
                        "event": "moderation_purge_failed",
                        "guild_id": guild.id,
                        "channel_id": channel_id,
                        "user_id": author_id,
                    },
                )
        self._recent_message_channels.pop((guild.id, author_id), None)
        return purged

    def _record_image_post(self, message: discord.Message, signal: MessageSignal) -> None:
        images = extract_image_attachments(signal.attachments)
        if not images:
            return
        now = time.monotonic()
        keep_for = self._settings().moderation_image_burst_window_seconds + IMAGE_BURST_CONFIRM_SECONDS
        self._recent_images = {
            key: posts
            for key, posts in self._recent_images.items()
            if any(now - post.posted_at <= keep_for for post in posts.values())
        }
        posts = self._recent_images.setdefault((signal.guild_id, signal.author_id), {})
        # Keyed by message id so a re-inspected message can never look like a repost of itself.
        posts[message.id] = ImagePost(
            posted_at=now,
            channel_id=signal.channel_id,
            signatures=tuple(image_signature(image) for image in images),
        )

    async def _confirm_then_apply_image_burst(self, message: discord.Message, decision: ModerationDecision) -> None:
        """Image bursts wait a few seconds and get a second look before anyone is punished."""
        key = (message.guild.id, message.author.id)
        if key in self._incidents:
            await self._apply_decision(message, decision)  # already confirmed: keep enforcing
            return
        pending = self._pending_image_bursts.get(key)
        if pending is not None:
            pending.append(message)
            return
        self._pending_image_bursts[key] = [message]
        try:
            await asyncio.sleep(IMAGE_BURST_CONFIRM_SECONDS)
        finally:
            messages = self._pending_image_bursts.pop(key, [message])

        settings = self._settings()
        confirmed_by = confirm_image_burst(
            tuple(self._recent_images.get(key, {}).values()),
            now=time.monotonic(),
            window_seconds=settings.moderation_image_burst_window_seconds + IMAGE_BURST_CONFIRM_SECONDS,
            min_images=settings.moderation_image_burst_count,
        )
        if confirmed_by is None:
            log.info(
                "Image burst not confirmed; no action taken",
                extra={
                    "event": "moderation_image_burst_unconfirmed",
                    "guild_id": key[0],
                    "user_id": key[1],
                    "details": decision.details,
                },
            )
            return
        decision = replace(decision, details=f"{decision.details} ({confirmed_by})")
        # Hash before enforcing: enforcement purges these messages, and a purged message's
        # attachments may no longer be reachable.
        image_hashes = await self._hash_flagged_images(messages)
        for flagged in messages:
            await self._apply_decision(flagged, decision)
        if image_hashes:
            incident = self._incidents.get(key)
            if incident is not None:
                await self._add_image_hashes(incident, image_hashes)

    @staticmethod
    async def _hash_flagged_images(messages: list[discord.Message]) -> tuple[int, ...]:
        attachments = [
            attachment
            for flagged in messages
            for attachment in flagged.attachments
            if scam_images.is_hashable(attachment)
        ]
        if not attachments:
            return ()
        hashes = await asyncio.gather(*(scam_images.hash_attachment(attachment) for attachment in attachments))
        return tuple(dict.fromkeys(value for value in hashes if value is not None))

    async def _inspect_message(self, message: discord.Message, *, is_edit: bool = False) -> None:
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

        if not is_edit:
            self._record_recent_channel(message)
            self._record_image_post(message, signal)
        off = self._filters_off(message)
        decision = evaluate_message(
            signal,
            self._decision_config(off),
            # Edits only re-check content (a link edited in); counting them toward
            # bursts again would double-count and punish someone fixing a typo.
            ModerationState() if is_edit else self._state,
            now=time.monotonic(),
        )
        if decision.action is ModerationAction.ALLOW:
            phishdestroy_decision = await self._evaluate_phishdestroy(signal, off)
            if phishdestroy_decision is not None:
                decision = phishdestroy_decision

        scam_hash_value: int | None = None
        if not is_edit and scam_check_applies(decision, enforcing=self._settings().moderation_scam_images_enforce):
            scam_hit = await self._evaluate_scam_images(message, off)
            if scam_hit is not None:
                decision, scam_hash_value = scam_hit

        decision = apply_rule_action(
            decision,
            parse_filter_rules(self._settings().moderation_filter_rules),
            timeout_seconds=HUMAN_ESCALATION_TIMEOUT_SECONDS,
        )
        if decision.reason == IMAGE_BURST_REASON:
            await self._confirm_then_apply_image_burst(message, decision)
            return
        await self._apply_decision(message, decision)
        if scam_hash_value is not None:
            incident = self._incidents.get((message.guild.id, message.author.id))
            if incident is not None:
                await self._add_image_hashes(incident, (scam_hash_value,))

    async def _evaluate_scam_images(
        self, message: discord.Message, off: frozenset[str] | None = None
    ) -> tuple[ModerationDecision, int] | None:
        """Hashes the message's hashable images and checks them against the known scam list.
        Shadow mode (moderation_scam_images_enforce=False) only alerts; enforce=True deletes."""
        settings = self._settings()
        if (
            not settings.moderation_scam_images_enabled
            or "scam_image" in (self._filters_off() if off is None else off)
            or scam_images.is_empty()
        ):
            return None
        hashable = [attachment for attachment in message.attachments if scam_images.is_hashable(attachment)]
        if not hashable:
            return None
        hashes = await asyncio.gather(*(scam_images.hash_attachment(attachment) for attachment in hashable))
        for value in hashes:
            if value is None:
                continue
            hash_id = scam_images.match(value, settings.moderation_scam_image_distance)
            if hash_id is None:
                continue
            bits = scam_images.distance(scam_images._hashes[hash_id], value)
            try:
                await scam_images.note_hit(hash_id)
            except Exception:
                log.exception(
                    "Failed to record a scam-image hit",
                    extra={"event": "scam_image_note_hit_failed", "hash_id": hash_id},
                )
            action = ModerationAction.DELETE if settings.moderation_scam_images_enforce else ModerationAction.ALERT
            decision = ModerationDecision(
                action=action,
                reason="scam_image",
                details=f"matches known scam image #{hash_id} ({bits} bits apart)",
                scam_hash_id=hash_id,
            )
            return decision, value
        return None

    async def _evaluate_phishdestroy(
        self, signal: MessageSignal, off: frozenset[str] | None = None
    ) -> ModerationDecision | None:
        if self._phishdestroy is None or self._phishdestroy_down:
            return None
        if "phishdestroy_domain" in (self._filters_off() if off is None else off):
            return None
        domains = tuple(sorted({url.domain for url in extract_urls(signal.content)}))
        for domain in domains:
            if (
                classify_domain(domain, allowed_domains=tuple(self._settings().moderation_allowed_domains))
                is DomainClassification.ALLOWED
            ):
                continue
            try:
                verdict = await self._phishdestroy.check_domain(domain)
            except PhishDestroyUnavailable as error:
                self._mark_phishdestroy_down(error)
                return None
            if verdict.threat:
                return self._phishdestroy_decision(domain, verdict)
        return None

    def _phishdestroy_decision(self, domain: str, verdict: PhishDestroyVerdict) -> ModerationDecision:
        defanged = defang_domain(domain)
        details = f"PhishDestroy threat match for {defanged}"
        if verdict.risk_score:
            details = f"{details} (risk {verdict.risk_score})"
        return ModerationDecision(
            action=self._phishdestroy_action(),
            reason="phishdestroy_domain",
            details=details,
            source="phishdestroy",
            domains=(domain,),
            defanged_domains=(defanged,),
        )

    def _mark_phishdestroy_down(self, error: Exception) -> None:
        if self._phishdestroy_down:
            return
        self._phishdestroy_down = True
        log.warning(
            "PhishDestroy API is unavailable; phishing API checks are paused",
            extra={
                "discord_forward": True,
                "event": "phishdestroy_api_down",
                "exception_type": type(error).__name__,
            },
        )
        self.recover_phishdestroy.change_interval(
            seconds=max(60, self._settings().phishdestroy_recovery_interval_seconds),
        )
        if not self.recover_phishdestroy.is_running():
            self.recover_phishdestroy.start()

    def _mark_phishdestroy_up(self) -> None:
        if not self._phishdestroy_down:
            return
        self._phishdestroy_down = False
        log.warning(
            "PhishDestroy API recovered; phishing API checks are active again",
            extra={
                "discord_forward": True,
                "event": "phishdestroy_api_recovered",
            },
        )
        if self.recover_phishdestroy.is_running():
            self.recover_phishdestroy.stop()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._inspect_message(message)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._scam_images_loaded:
            return
        self._scam_images_loaded = True
        try:
            count = await scam_images.load()
        except Exception:
            log.exception("Failed to load scam image hashes", extra={"event": "scam_images_load_failed"})
            return
        log.info(
            "Loaded %d known scam image hash(es)",
            count,
            extra={"event": "scam_images_loaded", "count": count},
        )

    def cog_unload(self) -> None:
        self.recover_phishdestroy.cancel()

    @tasks.loop(minutes=5)
    async def recover_phishdestroy(self) -> None:
        if self._phishdestroy is None or not self._phishdestroy_down:
            return
        try:
            await self._phishdestroy.healthcheck()
        except asyncio.CancelledError:
            raise
        except PhishDestroyUnavailable:
            return
        except Exception as error:
            log.debug("PhishDestroy recovery check failed: %s", error)
            return
        self._mark_phishdestroy_up()

    @recover_phishdestroy.before_loop
    async def _before_recover_phishdestroy(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content and before.attachments == after.attachments:
            return
        await self._inspect_message(after, is_edit=True)


def setup(bot: discord.Bot):
    bot.add_cog(ModerationCog(bot))
