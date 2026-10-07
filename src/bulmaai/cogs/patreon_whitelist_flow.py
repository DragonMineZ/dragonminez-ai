import hashlib
import logging
import time
import json
import asyncio
import html
import re
import secrets
from dataclasses import dataclass
from hmac import compare_digest
from urllib.parse import urlparse

import discord
from discord.ext import commands, tasks
from requests import HTTPError

from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services.discord_oauth import (
    DiscordOAuthClient,
    build_discord_authorization_url,
    build_discord_oauth_state,
    parse_discord_oauth_state,
)
from bulmaai.services import patron_page
from bulmaai.services.mojang import minecraft_username_exists
from bulmaai.services.patreon_access import (
    PatreonCreatorClient,
    PatreonOAuthClient,
    build_patreon_authorization_url,
    build_patreon_oauth_state,
    is_active_entitled_patron,
    parse_member_resource_status,
    parse_patreon_oauth_state,
    patreon_link_confirm_token,
    verify_patreon_webhook_signature,
)
from bulmaai.services.patreon_grants import (
    PatreonGrant,
    PatreonGrantKind,
    PatreonLink,
    count_active_gifts_for_owner,
    deactivate_gift_grant,
    deactivate_grants_for_owner,
    get_patreon_link,
    get_patreon_link_by_member_id,
    list_active_grants_for_owner,
    list_linked_owner_ids_with_active_grants,
    update_link_entitlement,
    upsert_patreon_link,
    upsert_whitelist_grant,
)
from bulmaai.services.release_webhook import (
    ReleaseWebhookHttpResponse,
    register_extra_get_route,
    register_extra_raw_webhook_route,
    text_http_response,
    unregister_extra_get_route,
    unregister_extra_raw_webhook_route,
)
from bulmaai.ui.patreon_views import (
    BetaAccessUsernameModal,
    MC_NAME_RE,
    PATREON_WELCOME_VERIFY_CUSTOM_ID,
    UsernameUpdateConfirmView,
)
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import has_any_allowed_role, has_patreon_access_role

log = logging.getLogger(__name__)

PATREON_OAUTH_TTL_SECONDS = 10 * 60
PATREON_WEBHOOK_PATH = "/patreon/webhook"
BETA_ACCESS_ROUTE_PREFIX = "/beta-access/"
BETA_ACCESS_START_PATH = "/beta-access/start"
BETA_ACCESS_NONCE_COOKIE = "dmz_beta_access_nonce"
DISCORD_OAUTH_TTL_SECONDS = 10 * 60
PROCESSED_OAUTH_STATE_TTL_SECONDS = PATREON_OAUTH_TTL_SECONDS + 60
# Patreon's Discord integration can swap tier roles (Contributor -> Benefactor) in two separate
# member updates, so a lost role is only acted on if it's still gone after this grace period.
ROLE_LOSS_GRACE_SECONDS = 120
PATREON_SYNC_INTERVAL_HOURS = 1
URL_RE = re.compile(r"https?://[^\s<]+")
# Whitelist file layout: staff-managed names sit above this header, bot adds land below it (appended).
AUTO_SECTION_HEADER = "# Auto-added"


@dataclass(frozen=True, slots=True)
class AutoApprovalResult:
    pr_url: str | None
    approved: bool


def _is_tester(member: discord.Member, settings) -> bool:
    return has_any_allowed_role(member, settings.dev_jar_tester_role_ids)


def _manual_whitelist_keys(lines: list[str]) -> set[str]:
    """Names above the auto-added header were added by staff; the bot must never remove them."""
    if not any(line.casefold().startswith(AUTO_SECTION_HEADER.casefold()) for line in lines):
        return set()
    keys = set()
    for line in lines:
        if line.casefold().startswith(AUTO_SECTION_HEADER.casefold()):
            break
        if not line.startswith("#"):
            keys.add(line.casefold())
    return keys


def _branch_key(*user_ids: int) -> str:
    # The whitelist repo is public, so branch names carry a stable hash instead of raw Discord IDs.
    return hashlib.sha256("-".join(map(str, user_ids)).encode()).hexdigest()[:12]


def _patreon_branch_name(user_id: int) -> str:
    return f"patreon/user-{_branch_key(user_id)}"


def _patreon_gift_branch_name(owner_id: int, recipient_id: int) -> str:
    return f"patreon/gift-{_branch_key(owner_id, recipient_id)}"


def _patreon_gift_edit_branch_name(owner_id: int, recipient_id: int) -> str:
    return f"patreon/gift-edit-{_branch_key(owner_id, recipient_id)}"


def _patreon_remove_branch_name(owner_id: int) -> str:
    return f"patreon/remove-{_branch_key(owner_id)}"


def _cookie_value(headers, name: str) -> str:
    for part in ((headers or {}).get("Cookie") or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def _eligible_tier_ids(settings) -> tuple[str, ...]:
    return tuple(str(tier_id) for tier_id in settings.patreon_eligible_tier_ids)


def _gift_limit_for_member(
    member: discord.Member, *, contributor_role_id: int | None, benefactor_role_id: int | None
) -> int:
    role_ids = {role.id for role in getattr(member, "roles", [])}
    if benefactor_role_id in role_ids:
        return 2
    if contributor_role_id in role_ids:
        return 1
    return 0


def _is_active_link(link: PatreonLink, settings) -> bool:
    return link.entitlement_active


def _github_error_status(exc: HTTPError) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return int(status_code) if isinstance(status_code, int) else None


def _is_recoverable_merge_error(exc: HTTPError) -> bool:
    return _github_error_status(exc) in {405, 409}


def _add_nickname_mutate(nickname: str):
    key = nickname.casefold()

    def mutate(lines: list[str]) -> list[str] | None:
        if any(line.casefold() == key for line in lines):
            return None
        return lines + [nickname]

    return mutate


def _rename_nickname_mutate(old_nickname: str, new_nickname: str):
    old_key = old_nickname.casefold()
    new_key = new_nickname.casefold()

    def mutate(lines: list[str]) -> list[str] | None:
        has_old = any(line.casefold() == old_key for line in lines)
        has_new = any(line.casefold() == new_key for line in lines)
        if has_new and not has_old:
            return None
        updated = [line for line in lines if line.casefold() != old_key]
        if not any(line.casefold() == new_key for line in updated):
            updated.append(new_nickname)
        return None if updated == lines else updated

    return mutate


def _active_self_grant(grants: list[PatreonGrant], member_id: int) -> PatreonGrant | None:
    for grant in grants:
        if (
            grant.active
            and grant.kind == PatreonGrantKind.SELF
            and grant.owner_discord_user_id == member_id
            and grant.beneficiary_discord_user_id == member_id
            and grant.minecraft_username
        ):
            return grant
    return None


def _discord_card(member, fallback_user_id: int) -> patron_page.DiscordCard:
    if member is None:
        return patron_page.DiscordCard(display_name=str(fallback_user_id), username=str(fallback_user_id))
    username = getattr(member, "name", None) or str(member)
    return patron_page.DiscordCard(
        display_name=getattr(member, "display_name", None) or username,
        username=username,
        avatar_url=getattr(getattr(member, "display_avatar", None), "url", None),
    )


async def _send_message(destination, content: str, *, ephemeral: bool = False, **kwargs) -> None:
    if ephemeral:
        kwargs["ephemeral"] = True
    await destination.send(content, **kwargs)


class BrowserFlowDestination:
    def __init__(self) -> None:
        self.messages: list[str] = []
        # Set by the flow so the browser page can show the right outcome instead of parsing messages.
        self.whitelisted: str | None = None
        self.patreon_url: str | None = None

    async def send(self, content: str, **kwargs) -> None:
        self.messages.append(str(content))


async def _pick_staff_channel(
    bot: discord.Bot,
    ctx_or_inter: discord.Interaction | discord.ApplicationContext | None = None,
    *,
    staff_channel_id: int | None = None,
) -> discord.abc.Messageable | None:
    if staff_channel_id:
        channel = bot.get_channel(staff_channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(staff_channel_id)
            except Exception:
                log.exception("Failed to fetch Patreon staff log channel %s", staff_channel_id)
                channel = None
        if channel is not None and hasattr(channel, "send"):
            return channel

    if ctx_or_inter is not None and not isinstance(ctx_or_inter.channel, discord.DMChannel):
        return ctx_or_inter.channel

    return None


class PatreonWhitelistFlowCog(ReloadableCog):
    """Patreon beta whitelist workflow used by the /patreon beta-access command."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.gh = self._build_github_service()
        self._beta_access_locks: dict[int, asyncio.Lock] = {}
        self._patreon_oauth_state_locks: dict[str, asyncio.Lock] = {}
        self._processed_patreon_oauth_states: dict[str, float] = {}

    def _build_github_service(self) -> GitHubService:
        settings = self.bot.settings
        auth = GitHubAppAuth(
            app_id=settings.GH_APP_ID,
            installation_id=settings.GH_INSTALLATION_ID,
            private_key_pem=settings.GH_APP_PRIVATE_KEY_PEM.replace("\\n", "\n"),
        )
        return GitHubService(
            auth=auth,
            owner=settings.GITHUB_OWNER,
            repo=settings.GITHUB_WHITELIST_REPO,
            base_branch=settings.GITHUB_BASE_BRANCH,
            whitelist_file_path=settings.GITHUB_WHITELIST_FILE_PATH,
        )

    def _ensure_runtime_state(self) -> None:
        if not hasattr(self, "_beta_access_locks"):
            self._beta_access_locks = {}
        if not hasattr(self, "_patreon_oauth_state_locks"):
            self._patreon_oauth_state_locks = {}
        if not hasattr(self, "_processed_patreon_oauth_states"):
            self._processed_patreon_oauth_states = {}

    def _beta_access_lock(self, member_id: int) -> asyncio.Lock:
        self._ensure_runtime_state()
        key = int(member_id)
        lock = self._beta_access_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._beta_access_locks[key] = lock
        return lock

    def _patreon_oauth_state_lock(self, state: str) -> asyncio.Lock:
        self._ensure_runtime_state()
        self._prune_processed_patreon_oauth_states()
        lock = self._patreon_oauth_state_locks.get(state)
        if lock is None:
            lock = asyncio.Lock()
            self._patreon_oauth_state_locks[state] = lock
        return lock

    def _prune_processed_patreon_oauth_states(self) -> None:
        now = time.monotonic()
        expired_states = [
            state
            for state, processed_at in self._processed_patreon_oauth_states.items()
            if now - processed_at > PROCESSED_OAUTH_STATE_TTL_SECONDS
        ]
        for state in expired_states:
            self._processed_patreon_oauth_states.pop(state, None)
            self._patreon_oauth_state_locks.pop(state, None)

    def _patreon_oauth_state_processed(self, state: str) -> bool:
        self._ensure_runtime_state()
        self._prune_processed_patreon_oauth_states()
        return state in self._processed_patreon_oauth_states

    def _mark_patreon_oauth_state_processed(self, state: str) -> None:
        self._ensure_runtime_state()
        self._processed_patreon_oauth_states[state] = time.monotonic()

    patreon = discord.SlashCommandGroup("patreon", "Patreon beta access")

    @patreon.command(
        name="beta-access",
        description="Request DragonMineZ Patreon beta access for a Minecraft username",
    )
    @discord.option(
        "username",
        description="Your Minecraft username",
        required=True,
    )
    async def beta_access(self, ctx: discord.ApplicationContext, username: str) -> None:
        await self._handle_beta_access_command(ctx, username)

    @patreon.command(name="link", description="Link your Patreon account")
    async def link_patreon(self, ctx: discord.ApplicationContext) -> None:
        await self._handle_link_patreon_command(ctx)

    @patreon.command(
        name="gift",
        description="Gift your Patreon beta access to another Discord member's Minecraft username",
    )
    @discord.option(
        "recipient",
        description="Discord member receiving beta access",
        required=True,
    )
    @discord.option(
        "username",
        description="Recipient Minecraft username",
        required=True,
    )
    async def gift_beta(
        self,
        ctx: discord.ApplicationContext,
        recipient: discord.Member,
        username: str,
    ) -> None:
        await self._handle_gift_beta_command(ctx, recipient, username)

    @patreon.command(
        name="edit-gift",
        description="Reassign a Patreon beta gift to another member and/or change the Minecraft username",
    )
    @discord.option(
        "recipient",
        description="The Discord member you currently have a gift for",
        required=True,
    )
    @discord.option(
        "new_recipient",
        description="Move the gift to this Discord member instead (optional)",
        required=False,
        default=None,
    )
    @discord.option(
        "username",
        description="New Minecraft username for the gift (optional)",
        required=False,
        default=None,
    )
    async def edit_gift(
        self,
        ctx: discord.ApplicationContext,
        recipient: discord.Member,
        new_recipient: discord.Member | None = None,
        username: str | None = None,
    ) -> None:
        await self._handle_edit_gift_command(ctx, recipient, new_recipient, username)

    async def on_startup(self) -> None:
        self._register_patreon_routes()
        if not self.sync_patreon_access.is_running():
            self.sync_patreon_access.start()

    async def on_shutdown(self) -> None:
        self.sync_patreon_access.cancel()
        self._unregister_patreon_routes()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        settings = self.bot.settings
        if not has_patreon_access_role(before, settings=settings) or has_patreon_access_role(after, settings=settings):
            return
        await asyncio.sleep(ROLE_LOSS_GRACE_SECONDS)
        member = await self._resolve_member_across_guilds(after.id)
        if member is not None and has_patreon_access_role(member, settings=settings):
            return
        try:
            await self.revoke_owner_access(after.id, "Patreon role removed")
        except Exception:
            log.exception("Failed to revoke Patreon beta access after role loss for %s", after.id)

    @tasks.loop(hours=PATREON_SYNC_INTERVAL_HOURS)
    async def sync_patreon_access(self) -> None:
        """Safety net for missed webhooks / role events: re-check every linked owner holding active grants."""
        try:
            owner_ids = await list_linked_owner_ids_with_active_grants()
        except Exception:
            log.exception("Patreon access sync could not list owners")
            return
        for owner_id in owner_ids:
            try:
                reason = await self._lapsed_access_reason(owner_id)
                if reason is not None:
                    await self.revoke_owner_access(owner_id, reason)
            except Exception:
                log.exception("Patreon access sync failed for owner %s", owner_id)
            await asyncio.sleep(1)  # ponytail: crude Patreon/GitHub rate limiting, fine for a few hundred patrons

    @sync_patreon_access.before_loop
    async def _before_sync_patreon_access(self) -> None:
        await self.bot.wait_until_ready()

    async def _lapsed_access_reason(self, owner_id: int) -> str | None:
        """Why this owner's grants should go, or None to keep them."""
        settings = self.bot.settings
        member = await self._resolve_member_across_guilds(owner_id)
        if member is None:
            return "left the Discord server"
        if not has_patreon_access_role(member, settings=settings):
            return "Patreon role missing"
        keep, reason = await self._patreon_verdict(owner_id)
        return None if keep is not False else reason

    async def _patreon_verdict(self, owner_id: int) -> tuple[bool | None, str | None]:
        """Ask Patreon itself: (True, None) active or lookup failed, (False, reason) lapsed, (None, None) no link."""
        settings = self.bot.settings
        link = await get_patreon_link(owner_id)
        if link is None or not link.patreon_member_id or not settings.PATREON_CREATOR_TOKEN:
            return None, None
        client = PatreonCreatorClient(
            creator_token=settings.PATREON_CREATOR_TOKEN,
            campaign_id=settings.PATREON_CAMPAIGN_ID,
        )
        try:
            status = await client.fetch_member_status(link.patreon_member_id)
        except HTTPError as exc:
            if _github_error_status(exc) != 404:
                log.warning("Patreon sync lookup failed for owner %s: %s", owner_id, exc)
                return True, None
            await update_link_entitlement(
                discord_user_id=owner_id,
                patron_status="deleted",
                tier_ids=(),
                last_charge_date=link.last_charge_date,
                entitlement_active=False,
            )
            return False, "Patreon membership deleted"
        except Exception as exc:
            log.warning("Patreon sync lookup failed for owner %s: %s", owner_id, exc)
            return True, None

        active = is_active_entitled_patron(status, eligible_tier_ids=_eligible_tier_ids(settings))
        await update_link_entitlement(
            discord_user_id=owner_id,
            patron_status=status.patron_status,
            tier_ids=status.tier_ids,
            last_charge_date=status.last_charge_date,
            entitlement_active=active,
        )
        return (True, None) if active else (False, f"Patreon status `{status.patron_status}`")

    async def revoke_owner_access(self, owner_id: int, reason: str | None) -> list[PatreonGrant]:
        """Remove the owner's self grant AND every gift they handed out. The whitelist file goes first so a
        GitHub failure leaves the grants active for the next webhook/sync to retry."""
        member = await self._resolve_member_across_guilds(owner_id)
        if member is not None and _is_tester(member, self.bot.settings):
            return []  # testers keep beta access without Patreon
        keep, _reason = await self._patreon_verdict(owner_id)
        if keep:
            # Roles can lag or glitch; never revoke someone Patreon still reports as an active patron.
            log.warning("Skipped revoking %s (%s): Patreon reports active or could not be checked", owner_id, reason)
            return []
        async with self._beta_access_lock(owner_id):
            grants = await list_active_grants_for_owner(owner_id)
            if not grants:
                return []
            await self._remove_whitelist_grants(owner_id, grants, reason)
            await deactivate_grants_for_owner(owner_id)
        return grants

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id != PATREON_WELCOME_VERIFY_CUSTOM_ID:
            return
        await interaction.response.send_modal(
            BetaAccessUsernameModal(on_submit=self._handle_welcome_verify_submit)
        )

    async def _handle_welcome_verify_submit(
        self,
        interaction: discord.Interaction,
        username: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        user = interaction.user
        member = user if isinstance(user, discord.Member) else None
        if member is None:
            member = await self._resolve_member_across_guilds(user.id)
        if member is None:
            await interaction.followup.send(
                "Join the DragonMineZ Discord server first, then press the button again.",
                ephemeral=True,
            )
            return

        await self.start_whitelist_flow_for_user(
            member,
            interaction.followup,
            username,
            ephemeral=True,
        )

    def _unregister_patreon_routes(self) -> None:
        unregister_extra_get_route(urlparse(self.bot.settings.patreon_oauth_redirect_uri).path)
        unregister_extra_get_route(BETA_ACCESS_ROUTE_PREFIX)
        unregister_extra_raw_webhook_route(PATREON_WEBHOOK_PATH)

    def _register_patreon_routes(self) -> None:
        # register_extra_get_route replaces any entry with the same prefix, so re-registering after a reload
        # swaps in this instance's handlers instead of stacking duplicates.
        loop = asyncio.get_running_loop()
        callback_path = urlparse(self.bot.settings.patreon_oauth_redirect_uri).path
        discord_callback_path = urlparse(self.bot.settings.discord_oauth_redirect_uri).path

        def handle_oauth_callback(path: str, query: dict[str, list[str]]) -> ReleaseWebhookHttpResponse:
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not code or not state:
                return text_http_response(400, "Missing OAuth code or state")
            confirm = (query.get("confirm") or [""])[0]
            future = asyncio.run_coroutine_threadsafe(
                self._handle_patreon_oauth_callback(code, state, confirm=confirm),
                loop,
            )
            try:
                return future.result(timeout=30)
            except Exception:
                log.exception("Patreon OAuth callback handling failed")
                return text_http_response(500, "Patreon authorization failed")

        def handle_webhook(body: bytes, headers) -> ReleaseWebhookHttpResponse:
            future = asyncio.run_coroutine_threadsafe(
                self._handle_patreon_webhook(body, headers),
                loop,
            )
            try:
                return future.result(timeout=30)
            except Exception:
                log.exception("Patreon webhook handling failed")
                return text_http_response(500, "Patreon webhook failed")

        def handle_beta_access(path: str, query: dict[str, list[str]], headers) -> ReleaseWebhookHttpResponse:
            if path.startswith(patron_page.ASSET_PREFIX):
                return patron_page.asset_response(path) or text_http_response(404, "Not found")
            if path == BETA_ACCESS_START_PATH:
                return self._handle_beta_access_start(query)
            if path != discord_callback_path:
                return text_http_response(404, "Not found")

            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not code or not state:
                return self._html_response("Missing Discord OAuth code or state.", status=400)
            future = asyncio.run_coroutine_threadsafe(
                self._handle_beta_access_discord_callback(
                    code=code,
                    state=state,
                    nonce=_cookie_value(headers, BETA_ACCESS_NONCE_COOKIE),
                ),
                loop,
            )
            try:
                return future.result(timeout=30)
            except Exception:
                log.exception("Beta access Discord OAuth callback handling failed")
                return self._html_response("Discord verification failed.", status=500)

        register_extra_get_route(
            path_prefix=callback_path,
            handle_request=handle_oauth_callback,
        )
        register_extra_get_route(
            path_prefix=BETA_ACCESS_ROUTE_PREFIX,
            handle_request=handle_beta_access,
            pass_headers=True,
        )
        register_extra_raw_webhook_route(
            path=PATREON_WEBHOOK_PATH,
            handle_request=handle_webhook,
        )

    def _handle_beta_access_start(self, query: dict[str, list[str]]) -> ReleaseWebhookHttpResponse:
        username = (query.get("username") or [""])[0].strip()
        if not MC_NAME_RE.match(username):
            return self._html_response(
                "Invalid Minecraft username. Use 3-16 letters, numbers, or underscores.",
                status=400,
            )

        nonce = secrets.token_urlsafe(16)
        url = self._build_discord_oauth_url(username, nonce)
        if url is None:
            return self._html_response(
                "Discord verification is not configured yet. Ask staff to check the bot settings.",
                status=500,
            )

        cookie = (
            f"{BETA_ACCESS_NONCE_COOKIE}={nonce}; Max-Age={DISCORD_OAUTH_TTL_SECONDS}; "
            f"Path={BETA_ACCESS_ROUTE_PREFIX}; HttpOnly; Secure; SameSite=Lax"
        )
        return ReleaseWebhookHttpResponse(
            status=302,
            body=b"",
            headers=(("Location", url), ("Set-Cookie", cookie)),
        )

    def _build_discord_oauth_url(self, minecraft_username: str, nonce: str) -> str | None:
        settings = self.bot.settings
        if not settings.discord_oauth_client_id or not settings.discord_oauth_client_secret:
            return None
        state = build_discord_oauth_state(
            secret=settings.discord_oauth_client_secret,
            minecraft_username=minecraft_username,
            expires_at=int(time.time() + DISCORD_OAUTH_TTL_SECONDS),
            nonce=nonce,
        )
        return build_discord_authorization_url(
            client_id=settings.discord_oauth_client_id,
            redirect_uri=settings.discord_oauth_redirect_uri,
            state=state,
        )

    async def _handle_beta_access_discord_callback(
        self,
        *,
        code: str,
        state: str,
        nonce: str,
        now=time.time,
    ) -> ReleaseWebhookHttpResponse:
        settings = self.bot.settings
        if not settings.discord_oauth_client_id or not settings.discord_oauth_client_secret:
            return self._html_response(
                "Discord verification is not configured yet. Ask staff to check the bot settings.",
                status=500,
            )
        parsed_state = parse_discord_oauth_state(
            settings.discord_oauth_client_secret,
            state,
            now=now,
        )
        if parsed_state is None or not MC_NAME_RE.match(parsed_state.minecraft_username):
            return self._html_response("Discord verification expired. Please try again from Minecraft.", status=403)
        if not parsed_state.nonce or not compare_digest(nonce.encode(), parsed_state.nonce.encode()):
            return self._html_response(
                "This verification was started in another browser. Please try again from Minecraft.",
                status=403,
            )
        minecraft_username = parsed_state.minecraft_username

        try:
            discord_user_id = await DiscordOAuthClient(
                client_id=settings.discord_oauth_client_id,
                client_secret=settings.discord_oauth_client_secret,
                redirect_uri=settings.discord_oauth_redirect_uri,
            ).fetch_user_id_for_code(code)
        except Exception as error:
            log.warning("Discord OAuth identity fetch failed: %s", error)
            return self._html_response("Discord authorization failed. Please try again.", status=500)

        member = await self._resolve_member_across_guilds(discord_user_id)
        if member is None:
            return self._html_response(
                f"Join the DragonMineZ Discord server before verifying beta access. (Minecraft username: {minecraft_username})",
                status=403,
            )

        destination = BrowserFlowDestination()
        await self.start_whitelist_flow_for_user(
            member,
            destination,
            minecraft_username,
            ephemeral=False,
        )
        if destination.whitelisted:
            return self._whitelisted_page(destination.whitelisted, patreon_just_linked=False)
        if destination.patreon_url:
            return patron_page.page_response(
                title="Link your Patreon",
                body_html=(
                    "Discord verified. Now link the Patreon account with your <b>Contributor</b> or "
                    f"<b>Benefactor</b> pledge to whitelist <b>{html.escape(minecraft_username)}</b>."
                ),
                steps=(True, False, False),
                actions=patron_page.button("Continue with Patreon", destination.patreon_url),
            )
        message = destination.messages[-1] if destination.messages else "Verification request accepted."
        return self._html_response(f"{message} (Minecraft username: {minecraft_username})")

    async def _resolve_member_across_guilds(self, user_id: int) -> discord.Member | None:
        guilds = list(getattr(self.bot, "guilds", []) or [])
        members: list[discord.Member] = []
        seen_guild_ids: set[int] = set()

        def add_member(member: discord.Member | None) -> discord.Member | None:
            if member is None:
                return None
            guild_id = getattr(getattr(member, "guild", None), "id", None)
            if guild_id is not None:
                if guild_id in seen_guild_ids:
                    return None
                seen_guild_ids.add(guild_id)
            members.append(member)
            return member

        def pick_access_member() -> discord.Member | None:
            for member in members:
                if has_patreon_access_role(member, settings=self.bot.settings) or _is_tester(member, self.bot.settings):
                    return member
            return None

        for guild in guilds:
            add_member(guild.get_member(user_id))
        access_member = pick_access_member()
        if access_member is not None:
            return access_member

        for guild in guilds:
            try:
                add_member(await guild.fetch_member(user_id))
            except Exception:
                continue
            access_member = pick_access_member()
            if access_member is not None:
                return access_member
        return members[0] if members else None

    async def _handle_beta_access_command(
        self,
        ctx: discord.ApplicationContext,
        username: str,
    ) -> None:
        await ctx.defer(ephemeral=True)

        if not isinstance(ctx.author, discord.Member):
            await ctx.followup.send(
                "Use `/patreon beta-access` inside the DragonMineZ server so I can verify your Patreon role.",
                ephemeral=True,
            )
            return

        await self.start_whitelist_flow_for_user(
            ctx.author,
            ctx.followup,
            username,
            ephemeral=True,
        )

    async def _handle_link_patreon_command(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        if not isinstance(ctx.author, discord.Member):
            await ctx.followup.send(
                "Use `/patreon link` inside the DragonMineZ server.",
                ephemeral=True,
            )
            return
        if not has_patreon_access_role(ctx.author, settings=self.bot.settings):
            await ctx.followup.send(
                "**Heads up:** You don't have a Patreon beta access role yet. "
                "If you're a Patron, connect your Patreon account to Discord first so the role is granted automatically — "
                "otherwise this link won't activate your beta access: "
                "https://support.patreon.com/hc/en-us/articles/212052266-Getting-Discord-access\n\n"
                "If you've already done that and are still missing the role, you can proceed with linking below.",
                ephemeral=True,
            )
        await self._send_patreon_oauth_prompt(ctx.author, ctx.followup, ephemeral=True)

    def _build_patreon_oauth_url(
        self,
        member: discord.Member,
        *,
        action: str = "link",
        minecraft_username: str | None = None,
    ) -> str | None:
        settings = self.bot.settings
        if not settings.patreon_oauth_client_id or not settings.patreon_oauth_client_secret:
            return None
        guild_id = getattr(getattr(member, "guild", None), "id", None)
        if guild_id is None:
            return None
        state = build_patreon_oauth_state(
            secret=settings.patreon_oauth_client_secret,
            discord_user_id=member.id,
            guild_id=int(guild_id),
            action=action,
            expires_at=int(time.time() + PATREON_OAUTH_TTL_SECONDS),
            minecraft_username=minecraft_username,
        )
        return build_patreon_authorization_url(
            client_id=settings.patreon_oauth_client_id,
            redirect_uri=settings.patreon_oauth_redirect_uri,
            state=state,
        )

    async def _send_patreon_oauth_prompt(
        self,
        member: discord.Member,
        destination,
        *,
        ephemeral: bool,
        minecraft_username: str | None = None,
    ) -> None:
        action = "beta_access" if minecraft_username else "link"
        url = self._build_patreon_oauth_url(
            member,
            action=action,
            minecraft_username=minecraft_username,
        )
        if url is None:
            await _send_message(
                destination,
                "Patreon linking is not configured yet. Ask staff to check the bot settings.",
                ephemeral=ephemeral,
            )
            return
        if isinstance(destination, BrowserFlowDestination):
            destination.patreon_url = url
        if minecraft_username:
            await _send_message(
                destination,
                f"Authorize with Patreon to continue beta access verification for `{minecraft_username}`: {url}",
                ephemeral=ephemeral,
            )
            return
        await _send_message(
            destination,
            f"Authorize with Patreon to link your account: {url}",
            ephemeral=ephemeral,
        )

    async def start_whitelist_flow_for_user(
        self,
        member: discord.Member,
        destination,
        initial_nickname: str,
        *,
        ephemeral: bool = False,
        active_link: PatreonLink | None = None,
    ) -> None:
        """
        Core Patreon whitelist workflow used by /patreon beta-access.
        """
        nickname = initial_nickname.strip() if initial_nickname is not None else ""
        if not MC_NAME_RE.match(nickname):
            await _send_message(
                destination,
                "Invalid Minecraft username. Use 3-16 letters, numbers, or underscores.",
                ephemeral=ephemeral,
            )
            return

        if not has_patreon_access_role(member, settings=self.bot.settings) and not _is_tester(member, self.bot.settings):
            await _send_message(
                destination,
                "You don't have a Patreon beta access role yet. "
                "If you're a Patron, make sure to connect your Patreon account to Discord so the role is granted automatically: "
                "https://support.patreon.com/hc/en-us/articles/212052266-Getting-Discord-access",
                ephemeral=ephemeral,
            )
            return

        link = active_link or await get_patreon_link(member.id)
        if link is None or not _is_active_link(link, self.bot.settings):
            await self._send_patreon_oauth_prompt(
                member,
                destination,
                ephemeral=ephemeral,
                minecraft_username=nickname,
            )
            return

        async with self._beta_access_lock(member.id):
            self_grant = _active_self_grant(await list_active_grants_for_owner(member.id), member.id)
            if self_grant is not None:
                old_nickname = self_grant.minecraft_username.strip()
                if old_nickname.casefold() == nickname.casefold():
                    if isinstance(destination, BrowserFlowDestination):
                        destination.whitelisted = old_nickname
                    await _send_message(
                        destination,
                        f"`{nickname}` is already your active Patreon beta whitelist username.",
                        ephemeral=ephemeral,
                    )
                    return
                await self._send_username_update_prompt(
                    member,
                    destination,
                    old_nickname,
                    nickname,
                    ephemeral=ephemeral,
                )
                return

            mojang_ok = await self._check_mojang_username(nickname)

            try:
                approval = await self._auto_approve_beta_access(member, nickname)
            except Exception:
                log.exception(
                    "Failed to auto approve Patreon beta access",
                    extra={
                        "event": "patreon_beta_access_auto_approval_failed",
                        "user_id": member.id,
                        "nickname": nickname,
                    },
                )
                await _send_message(
                    destination,
                    "I could not submit the whitelist change. Please ask staff to check the bot logs.",
                    ephemeral=ephemeral,
                )
                return

            if approval.pr_url is None:
                if isinstance(destination, BrowserFlowDestination):
                    destination.whitelisted = nickname
                await _send_message(
                    destination,
                    f"`{nickname}` is already whitelisted. Nothing to do.",
                    ephemeral=ephemeral,
                )
                return

            if not approval.approved:
                await _send_message(
                    destination,
                    "Whitelist PR created, but GitHub would not auto-merge it yet. "
                    f"Staff can review it here: {approval.pr_url}",
                    ephemeral=ephemeral,
                )
                return

            await self._record_self_grant(member, nickname, approval.pr_url)
            if isinstance(destination, BrowserFlowDestination):
                destination.whitelisted = nickname
            await _send_message(
                destination,
                f"`{nickname}` was approved automatically for Patreon beta access.",
                ephemeral=ephemeral,
            )
            await self._log_staff_info(
                f"{member.mention} linked Patreon access and `{nickname}` was approved automatically.\n-# [GitHub PR](<{approval.pr_url}>)"
            )
            if mojang_ok is False:
                await self._flag_unresolved_mojang_username(
                    nickname=nickname,
                    member=member,
                    context="self beta access",
                )

    async def _send_username_update_prompt(
        self,
        member: discord.Member,
        destination,
        old_nickname: str,
        new_nickname: str,
        *,
        ephemeral: bool,
    ) -> None:
        if isinstance(destination, BrowserFlowDestination):
            await _send_message(
                destination,
                f"You are already whitelisted as `{old_nickname}`. "
                f"Run `/patreon beta-access username:{new_nickname}` in Discord to confirm updating your username.",
                ephemeral=ephemeral,
            )
            return

        async def confirm(update_inter: discord.Interaction):
            await self._confirm_username_update(
                member,
                update_inter,
                old_nickname,
                new_nickname,
            )

        await _send_message(
            destination,
            "Hey, you already are whitelisted, but we can update your username. "
            f"Your old username `{old_nickname}` will be changed to `{new_nickname}`. Continue?",
            ephemeral=ephemeral,
            view=UsernameUpdateConfirmView(
                requester_id=member.id,
                old_nickname=old_nickname,
                new_nickname=new_nickname,
                on_confirm=confirm,
            ),
        )

    async def _confirm_username_update(
        self,
        member: discord.Member,
        interaction: discord.Interaction,
        old_nickname: str,
        new_nickname: str,
    ) -> None:
        async with self._beta_access_lock(member.id):
            current_grant = _active_self_grant(await list_active_grants_for_owner(member.id), member.id)
            if current_grant is None:
                await interaction.followup.send(
                    "I could not find your active Patreon beta whitelist grant. Please run `/patreon beta-access` again.",
                    ephemeral=True,
                )
                return

            current_nickname = current_grant.minecraft_username.strip()
            if current_nickname.casefold() == new_nickname.casefold():
                await interaction.followup.send(
                    f"`{new_nickname}` is already your active Patreon beta whitelist username.",
                    ephemeral=True,
                )
                return
            if current_nickname.casefold() != old_nickname.casefold():
                await interaction.followup.send(
                    "Your active Patreon beta whitelist username changed while this confirmation was open. "
                    "Please run `/patreon beta-access` again.",
                    ephemeral=True,
                )
                return

            try:
                approval = await self._auto_update_beta_access(member, old_nickname, new_nickname)
            except Exception:
                log.exception(
                    "Failed to update Patreon beta access username",
                    extra={
                        "event": "patreon_beta_access_username_update_failed",
                        "user_id": member.id,
                        "old_nickname": old_nickname,
                        "new_nickname": new_nickname,
                    },
                )
                await interaction.followup.send(
                    "I could not submit the whitelist username update. Please ask staff to check the bot logs.",
                    ephemeral=True,
                )
                return

            if approval.pr_url is None:
                await interaction.followup.send(
                    f"`{new_nickname}` is already whitelisted. Nothing to update.",
                    ephemeral=True,
                )
                return

            if not approval.approved:
                await interaction.followup.send(
                    "Whitelist update PR created, but GitHub would not auto-merge it yet. "
                    f"Staff can review it here: {approval.pr_url}",
                    ephemeral=True,
                )
                return

            await self._record_self_grant(member, new_nickname, approval.pr_url)
            await interaction.followup.send(
                f"Your Patreon beta whitelist username was updated from `{old_nickname}` to `{new_nickname}`.",
                ephemeral=True,
            )
            await self._log_staff_info(
                f"{member.mention} updated Patreon beta access from `{old_nickname}` to `{new_nickname}`.\n-# [GitHub PR](<{approval.pr_url}>)"
            )

    async def _record_self_grant(self, member: discord.Member, nickname: str, pr_url: str | None) -> None:
        """DB bookkeeping after a self whitelist PR merged; shared with the admin panel."""
        await upsert_whitelist_grant(
            PatreonGrant(
                owner_discord_user_id=member.id,
                beneficiary_discord_user_id=member.id,
                beneficiary_discord_username=str(member),
                minecraft_username=nickname,
                kind=PatreonGrantKind.SELF,
                active=True,
                source_pr_url=pr_url,
            )
        )

    async def _auto_approve_beta_access(self, member: discord.Member, nickname: str) -> AutoApprovalResult:
        branch = _patreon_branch_name(member.id)
        pr_data = await self._create_whitelist_add_pr(
            branch=branch,
            nickname=nickname,
            title=f"Add beta tester: {nickname}",
            commit_message=f"Add beta tester: {nickname}",
            body=f"Automatically approved through Patreon OAuth for Discord user {member}.",
        )
        if pr_data is None:
            return AutoApprovalResult(pr_url=None, approved=False)
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]
        return await self._merge_auto_pr(
            member=member,
            nickname=nickname,
            branch=branch,
            pr_number=pr_number,
            pr_url=pr_url,
            success_comment=f"Automatically approved through Patreon OAuth for {member}.",
            pending_description="Automatic Patreon approval",
            rebase_mutate=_add_nickname_mutate(nickname),
            rebase_commit_message=f"Add beta tester: {nickname}",
        )

    async def _auto_update_beta_access(
        self,
        member: discord.Member,
        old_nickname: str,
        new_nickname: str,
    ) -> AutoApprovalResult:
        branch = _patreon_branch_name(member.id)
        pr_data = await self._create_whitelist_update_pr(
            branch=branch,
            old_nickname=old_nickname,
            new_nickname=new_nickname,
            title=f"Update beta tester: {old_nickname} -> {new_nickname}",
            commit_message=f"Update beta tester: {old_nickname} -> {new_nickname}",
            body=(
                f"Automatically updated through Patreon OAuth for Discord user {member}. "
                f"Replacing `{old_nickname}` with `{new_nickname}`."
            ),
        )
        if pr_data is None:
            return AutoApprovalResult(pr_url=None, approved=False)
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]
        return await self._merge_auto_pr(
            member=member,
            nickname=new_nickname,
            branch=branch,
            pr_number=pr_number,
            pr_url=pr_url,
            success_comment=(
                f"Automatically updated Patreon beta access for {member}: "
                f"{old_nickname} -> {new_nickname}."
            ),
            pending_description="Automatic Patreon username update",
            rebase_mutate=_rename_nickname_mutate(old_nickname, new_nickname),
            rebase_commit_message=f"Update beta tester: {old_nickname} -> {new_nickname}",
        )

    async def _merge_auto_pr(
        self,
        *,
        member: discord.Member,
        nickname: str,
        branch: str,
        pr_number: int,
        pr_url: str,
        success_comment: str,
        pending_description: str,
        rebase_mutate,
        rebase_commit_message: str,
    ) -> AutoApprovalResult:
        try:
            await self.gh.merge_pr(pr_number)
        except HTTPError as exc:
            if not _is_recoverable_merge_error(exc):
                raise
            current_pr = await self.gh.get_pr(pr_number)
            if current_pr.get("merged"):
                await self.gh.remove_branch(branch)
                return AutoApprovalResult(pr_url=pr_url, approved=True)

            retry_result = await self._rebase_and_retry_merge(
                branch=branch,
                pr_number=pr_number,
                pr_url=pr_url,
                mutate=rebase_mutate,
                commit_message=rebase_commit_message,
                success_comment=success_comment,
                member=member,
                nickname=nickname,
            )
            if retry_result is not None:
                return retry_result

            await self._record_auto_merge_pending(
                member=member,
                nickname=nickname,
                pr_number=pr_number,
                pr_url=pr_url,
                status_code=_github_error_status(exc),
                description=pending_description,
            )
            return AutoApprovalResult(pr_url=pr_url, approved=False)
        await self.gh.add_pr_comment(
            pr_number,
            success_comment,
        )
        await self.gh.remove_branch(branch)
        return AutoApprovalResult(pr_url=pr_url, approved=True)

    async def _rebase_and_retry_merge(
        self,
        *,
        branch: str,
        pr_number: int,
        pr_url: str,
        mutate,
        commit_message: str,
        success_comment: str,
        member: discord.Member,
        nickname: str,
    ) -> AutoApprovalResult | None:
        """
        A stale-base merge conflict (405/409) means another whitelist PR merged
        first. Our edits are single-line adds/renames, so instead of a real
        3-way git merge we just reset the branch onto the new base and
        reapply the same line-level edit, then retry once. Returns None if
        the retry didn't resolve it, so the caller falls back to staff review.
        """
        try:
            base_sha = await self.gh.get_ref_sha(self.gh.base_branch)
            await self.gh.reset_branch(branch, base_sha)
            text, sha = await self.gh.get_whitelist_file(ref=branch)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            new_lines = mutate(lines)
            if new_lines is None:
                # The base already reflects the desired end state (someone
                # else's PR already made this exact change) - nothing to merge.
                await self.gh.close_pr(pr_number)
                await self.gh.remove_branch(branch)
                return AutoApprovalResult(pr_url=None, approved=True)
            await self.gh.put_whitelist_file(
                branch=branch,
                new_text="\n".join(new_lines) + "\n",
                sha=sha,
                message=commit_message,
            )
            await self.gh.merge_pr(pr_number)
        except HTTPError as exc:
            if not _is_recoverable_merge_error(exc):
                raise
            return None
        except Exception:
            log.exception(
                "Failed to rebase Patreon whitelist branch after merge conflict",
                extra={
                    "event": "patreon_whitelist_rebase_failed",
                    "user_id": member.id,
                    "branch": branch,
                    "nickname": nickname,
                },
            )
            return None

        await self.gh.add_pr_comment(pr_number, success_comment)
        await self.gh.remove_branch(branch)
        return AutoApprovalResult(pr_url=pr_url, approved=True)

    async def _record_auto_merge_pending(
        self,
        *,
        member: discord.Member,
        nickname: str,
        pr_number: int,
        pr_url: str,
        status_code: int | None,
        description: str,
    ) -> None:
        reason = f"HTTP {status_code}" if status_code is not None else "GitHub rejected the merge"
        comment = (
            f"{description} created this PR, but GitHub would not auto-merge it "
            f"({reason}). Staff should review and merge manually if the whitelist change is valid."
        )
        try:
            await self.gh.add_pr_comment(pr_number, comment)
        except Exception:
            log.exception(
                "Failed to comment on Patreon auto-merge pending PR",
                extra={
                    "event": "patreon_beta_access_pending_comment_failed",
                    "user_id": member.id,
                    "nickname": nickname,
                    "pr_number": pr_number,
                },
            )
        await self._log_staff_info(
            f"{member.mention} linked Patreon access and `{nickname}` has a whitelist PR, "
            f"but it could not be auto-merged ({reason}).\n-# [GitHub PR](<{pr_url}>)"
        )

    async def _create_whitelist_add_pr(
        self,
        *,
        branch: str,
        nickname: str,
        title: str,
        commit_message: str,
        body: str,
    ) -> dict | None:
        nickname_key = nickname.casefold()
        base_text, _base_sha = await self.gh.get_whitelist_file(ref=self.gh.base_branch)
        base_lines = [ln.strip() for ln in base_text.splitlines() if ln.strip()]
        if any(line.casefold() == nickname_key for line in base_lines):
            return None

        await self.gh.create_branch(branch, self.gh.base_branch)
        branch_text, branch_sha = await self.gh.get_whitelist_file(ref=branch)
        branch_lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
        if not any(line.casefold() == nickname_key for line in branch_lines):
            branch_lines.append(nickname)
            await self.gh.put_whitelist_file(
                branch=branch,
                new_text="\n".join(branch_lines) + "\n",
                sha=branch_sha,
                message=commit_message,
            )
        return await self.gh.create_or_get_pr(
            head_branch=branch,
            title=title,
            body=body,
        )

    async def _create_whitelist_update_pr(
        self,
        *,
        branch: str,
        old_nickname: str,
        new_nickname: str,
        title: str,
        commit_message: str,
        body: str,
    ) -> dict | None:
        base_text, _base_sha = await self.gh.get_whitelist_file(ref=self.gh.base_branch)
        base_lines = [ln.strip() for ln in base_text.splitlines() if ln.strip()]
        old_key = old_nickname.casefold()
        new_key = new_nickname.casefold()
        base_has_old = any(line.casefold() == old_key for line in base_lines)
        base_has_new = any(line.casefold() == new_key for line in base_lines)
        if not base_has_old and base_has_new:
            return None

        await self.gh.create_branch(branch, self.gh.base_branch)
        branch_text, branch_sha = await self.gh.get_whitelist_file(ref=branch)
        branch_lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
        updated_lines = [line for line in branch_lines if line.casefold() != old_key]
        if not any(line.casefold() == new_key for line in updated_lines):
            updated_lines.append(new_nickname)
        if updated_lines == branch_lines:
            return None
        await self.gh.put_whitelist_file(
            branch=branch,
            new_text="\n".join(updated_lines) + "\n",
            sha=branch_sha,
            message=commit_message,
        )
        return await self.gh.create_or_get_pr(
            head_branch=branch,
            title=title,
            body=body,
        )

    async def _handle_edit_gift_command(
        self,
        ctx: discord.ApplicationContext,
        recipient: discord.Member,
        new_recipient: discord.Member | None = None,
        username: str | None = None,
    ) -> None:
        await ctx.defer(ephemeral=True)

        if not isinstance(ctx.author, discord.Member):
            await ctx.followup.send(
                "Use `/patreon edit-gift` inside the DragonMineZ server.",
                ephemeral=True,
            )
            return

        if new_recipient is not None and new_recipient.bot:
            await ctx.followup.send("You cannot gift beta access to a bot.", ephemeral=True)
            return

        async with self._beta_access_lock(ctx.author.id):
            gift_grants = [
                g
                for g in await list_active_grants_for_owner(ctx.author.id)
                if g.kind == PatreonGrantKind.GIFT
            ]
            gift_grant = next(
                (g for g in gift_grants if g.beneficiary_discord_user_id == recipient.id),
                None,
            )
            # Fall back to the single gift so an owner can fix it regardless of which
            # Discord member they originally (mis)gifted to.
            if gift_grant is None and len(gift_grants) == 1:
                gift_grant = gift_grants[0]
            if gift_grant is None:
                if not gift_grants:
                    await ctx.followup.send(
                        "You don't have any active Patreon gift to edit.",
                        ephemeral=True,
                    )
                else:
                    await ctx.followup.send(
                        f"You don't have an active Patreon gift for {recipient.mention}. "
                        "Pick the member you currently have a gift for as `recipient`.",
                        ephemeral=True,
                    )
                return

            old_recipient_id = gift_grant.beneficiary_discord_user_id
            old_nickname = gift_grant.minecraft_username

            target = new_recipient if new_recipient is not None else recipient
            recipient_changed = target.id != old_recipient_id

            nickname = username.strip() if username is not None else old_nickname
            if not MC_NAME_RE.match(nickname):
                await ctx.followup.send(
                    "Invalid Minecraft username. Use 3-16 letters, numbers, or underscores.",
                    ephemeral=True,
                )
                return
            nickname_changed = old_nickname.casefold() != nickname.casefold()

            if not recipient_changed and not nickname_changed:
                await ctx.followup.send(
                    f"Nothing to change — {target.mention} already holds this gift as `{nickname}`.",
                    ephemeral=True,
                )
                return

            pr_url: str | None = None
            if nickname_changed:
                branch = _patreon_gift_edit_branch_name(ctx.author.id, target.id)
                try:
                    approval = await self._auto_update_gift_access(
                        owner=ctx.author,
                        recipient=target,
                        old_nickname=old_nickname,
                        new_nickname=nickname,
                        branch=branch,
                    )
                except Exception:
                    log.exception(
                        "Failed to update gifted Patreon beta access",
                        extra={
                            "event": "patreon_gift_edit_failed",
                            "owner_user_id": ctx.author.id,
                            "old_recipient_user_id": old_recipient_id,
                            "new_recipient_user_id": target.id,
                            "old_nickname": old_nickname,
                            "new_nickname": nickname,
                        },
                    )
                    await ctx.followup.send(
                        "I could not submit the username update. Please ask staff to check the bot logs.",
                        ephemeral=True,
                    )
                    return

                if approval.pr_url is None:
                    await ctx.followup.send(
                        f"`{nickname}` is already whitelisted. Nothing to update.",
                        ephemeral=True,
                    )
                    return

                if not approval.approved:
                    await ctx.followup.send(
                        "Username update PR created, but GitHub would not auto-merge it yet. "
                        f"Staff can review it here: {approval.pr_url}",
                        ephemeral=True,
                    )
                    return
                pr_url = approval.pr_url

            # Move the gift to the new recipient by retiring the old grant row before
            # writing the new one, so the owner's active gift count stays accurate.
            if recipient_changed:
                await deactivate_gift_grant(ctx.author.id, old_recipient_id)

            await upsert_whitelist_grant(
                PatreonGrant(
                    owner_discord_user_id=ctx.author.id,
                    beneficiary_discord_user_id=target.id,
                    beneficiary_discord_username=str(target),
                    minecraft_username=nickname,
                    kind=PatreonGrantKind.GIFT,
                    active=True,
                    source_pr_url=pr_url if pr_url is not None else gift_grant.source_pr_url,
                )
            )

        await ctx.followup.send(
            self._edit_gift_summary(
                recipient_changed=recipient_changed,
                nickname_changed=nickname_changed,
                old_recipient_id=old_recipient_id,
                target=target,
                old_nickname=old_nickname,
                nickname=nickname,
            ),
            ephemeral=True,
        )
        staff_note = (
            f"{ctx.author.mention} edited a gifted Patreon beta access "
            f"(recipient <@{old_recipient_id}> -> {target.mention}, "
            f"`{old_nickname}` -> `{nickname}`)."
        )
        if pr_url is not None:
            staff_note += f"\n-# [GitHub PR](<{pr_url}>)"
        await self._log_staff_info(staff_note)

    @staticmethod
    def _edit_gift_summary(
        *,
        recipient_changed: bool,
        nickname_changed: bool,
        old_recipient_id: int,
        target: discord.Member,
        old_nickname: str,
        nickname: str,
    ) -> str:
        if recipient_changed and nickname_changed:
            return (
                f"Moved the Patreon beta gift from <@{old_recipient_id}> to {target.mention} "
                f"and updated the Minecraft username from `{old_nickname}` to `{nickname}`."
            )
        if recipient_changed:
            return (
                f"Moved the Patreon beta gift from <@{old_recipient_id}> to {target.mention} "
                f"(Minecraft username `{nickname}`)."
            )
        return (
            f"Updated {target.mention}'s Patreon beta whitelist username "
            f"from `{old_nickname}` to `{nickname}`."
        )

    async def _auto_update_gift_access(
        self,
        *,
        owner: discord.Member,
        recipient: discord.Member,
        old_nickname: str,
        new_nickname: str,
        branch: str,
    ) -> AutoApprovalResult:
        pr_data = await self._create_whitelist_update_pr(
            branch=branch,
            old_nickname=old_nickname,
            new_nickname=new_nickname,
            title=f"Update gifted beta tester: {old_nickname} -> {new_nickname}",
            commit_message=f"Update gifted beta tester: {old_nickname} -> {new_nickname}",
            body=(
                f"Username update requested by Discord user {owner} "
                f"for gift recipient {recipient}. "
                f"Replacing `{old_nickname}` with `{new_nickname}`."
            ),
        )
        if pr_data is None:
            return AutoApprovalResult(pr_url=None, approved=False)
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]
        return await self._merge_auto_pr(
            member=owner,
            nickname=new_nickname,
            branch=branch,
            pr_number=pr_number,
            pr_url=pr_url,
            success_comment=(
                f"Gift username update by {owner}: "
                f"`{old_nickname}` -> `{new_nickname}` for {recipient}."
            ),
            pending_description="Gift username update",
            rebase_mutate=_rename_nickname_mutate(old_nickname, new_nickname),
            rebase_commit_message=f"Update gifted beta tester: {old_nickname} -> {new_nickname}",
        )

    async def _handle_gift_beta_command(
        self,
        ctx: discord.ApplicationContext,
        recipient: discord.Member,
        username: str,
    ) -> None:
        await ctx.defer(ephemeral=True)
        if not isinstance(ctx.author, discord.Member):
            await ctx.followup.send(
                "Use `/patreon gift` inside the DragonMineZ server.",
                ephemeral=True,
            )
            return
        if recipient.bot:
            await ctx.followup.send("You cannot gift beta access to a bot.", ephemeral=True)
            return

        link = await get_patreon_link(ctx.author.id)
        if link is None or not _is_active_link(link, self.bot.settings):
            await self._send_patreon_oauth_prompt(ctx.author, ctx.followup, ephemeral=True)
            return
        if not has_patreon_access_role(ctx.author, settings=self.bot.settings):
            await ctx.followup.send(
                "Your Patreon account is linked, but you need the DragonMineZ Patreon access role in this server before gifting beta access.",
                ephemeral=True,
            )
            return

        nickname = username.strip() if username is not None else ""
        if not MC_NAME_RE.match(nickname):
            await ctx.followup.send(
                "Invalid Minecraft username. Use 3-16 letters, numbers, or underscores.",
                ephemeral=True,
            )
            return

        gift_limit = _gift_limit_for_member(
            ctx.author,
            contributor_role_id=self.bot.settings.patreon_contributor_role_id,
            benefactor_role_id=self.bot.settings.patreon_benefactor_role_id,
        )
        async with self._beta_access_lock(ctx.author.id):
            used_gifts = await count_active_gifts_for_owner(ctx.author.id)
            if used_gifts >= gift_limit:
                await ctx.followup.send(
                    f"You have already used your active Patreon gift limit ({gift_limit}).",
                    ephemeral=True,
                )
                return

            mojang_ok = await self._check_mojang_username(nickname)

            try:
                approval = await self._auto_approve_gift_beta_access(ctx.author, recipient, nickname)
            except Exception:
                log.exception(
                    "Failed to submit Patreon gift beta access request",
                    extra={
                        "event": "patreon_gift_beta_request_failed",
                        "owner_user_id": ctx.author.id,
                        "recipient_user_id": recipient.id,
                        "nickname": nickname,
                    },
                )
                await ctx.followup.send(
                    "I could not submit the gift request. Please ask staff to check the bot logs.",
                    ephemeral=True,
                )
                return

            if approval.pr_url is None:
                await ctx.followup.send(
                    f"`{nickname}` is already whitelisted. Nothing to do.",
                    ephemeral=True,
                )
                return

            if not approval.approved:
                await ctx.followup.send(
                    "Gift PR created, but GitHub would not auto-merge it yet. "
                    f"Staff can review it here: {approval.pr_url}",
                    ephemeral=True,
                )
                return

            await upsert_whitelist_grant(
                PatreonGrant(
                    owner_discord_user_id=ctx.author.id,
                    beneficiary_discord_user_id=recipient.id,
                    beneficiary_discord_username=str(recipient),
                    minecraft_username=nickname,
                    kind=PatreonGrantKind.GIFT,
                    active=True,
                    source_pr_url=approval.pr_url,
                )
            )
        await ctx.followup.send(
            f"Gift approved automatically: {recipient.mention} as `{nickname}`.",
            ephemeral=True,
        )
        await self._log_staff_info(
            f"{ctx.author.mention} gifted Patreon beta access to {recipient.mention} as `{nickname}` "
            f"(auto-approved).\n-# [GitHub PR](<{approval.pr_url}>)"
        )
        if mojang_ok is False:
            await self._flag_unresolved_mojang_username(
                nickname=nickname,
                member=ctx.author,
                context=f"gifted to {recipient.mention}",
            )

    async def _auto_approve_gift_beta_access(
        self,
        owner: discord.Member,
        recipient: discord.Member,
        nickname: str,
    ) -> AutoApprovalResult:
        branch = _patreon_gift_branch_name(owner.id, recipient.id)
        pr_data = await self._create_whitelist_add_pr(
            branch=branch,
            nickname=nickname,
            title=f"Gift beta tester: {nickname}",
            commit_message=f"Gift beta tester: {nickname}",
            body=(
                f"Gift requested by Discord user {owner} "
                f"for {recipient}. Automatically approved."
            ),
        )
        if pr_data is None:
            return AutoApprovalResult(pr_url=None, approved=False)
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]
        return await self._merge_auto_pr(
            member=owner,
            nickname=nickname,
            branch=branch,
            pr_number=pr_number,
            pr_url=pr_url,
            success_comment=(
                f"Automatically approved gift from {owner} for {recipient}."
            ),
            pending_description="Automatic Patreon gift approval",
            rebase_mutate=_add_nickname_mutate(nickname),
            rebase_commit_message=f"Gift beta tester: {nickname}",
        )

    async def _check_mojang_username(self, nickname: str) -> bool | None:
        return await minecraft_username_exists(nickname)

    async def _flag_unresolved_mojang_username(
        self,
        *,
        nickname: str,
        member: discord.Member,
        context: str,
    ) -> None:
        channel = await _pick_staff_channel(
            self.bot, staff_channel_id=self.bot.settings.patreon_ai_log_channel_id
        )
        if channel is None:
            log.warning(
                "Patreon ai-log channel unavailable; `%s` did not resolve on Mojang (%s)",
                nickname,
                context,
            )
            return
        await channel.send(
            f"`{nickname}` ({context} by {member.mention}) did not resolve to an official "
            "Mojang/Minecraft account. Allowed anyway (Mojang lookup is warn-but-allow).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _log_staff_info(self, content: str) -> None:
        channel = await _pick_staff_channel(
            self.bot, staff_channel_id=self.bot.settings.patreon_staff_channel_id
        )
        if channel is None:
            log.warning("Patreon staff log channel unavailable: %s", content)
            return
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())

    async def _handle_patreon_oauth_callback(
        self, code: str, state: str, *, confirm: str = ""
    ) -> ReleaseWebhookHttpResponse:
        settings = self.bot.settings
        if not settings.patreon_oauth_client_id or not settings.patreon_oauth_client_secret:
            return text_http_response(500, "Patreon OAuth is not configured")
        parsed_state = parse_patreon_oauth_state(
            settings.patreon_oauth_client_secret,
            state,
            now=time.time,
        )
        if parsed_state is None:
            return self._expired_page()

        async with self._patreon_oauth_state_lock(state):
            if self._patreon_oauth_state_processed(state):
                return self._html_response(
                    "This Patreon authorization was already processed. You can close this tab and return to Discord."
                )
            expected = patreon_link_confirm_token(settings.patreon_oauth_client_secret, code, state)
            if not compare_digest(confirm.encode(), expected.encode()):
                return await self._patreon_link_confirm_page(code, state, expected, parsed_state)
            response = await self._complete_patreon_oauth_callback(code, parsed_state)
            if response.status < 500:
                self._mark_patreon_oauth_state_processed(state)
            return response

    async def _patreon_link_confirm_page(
        self, code: str, state: str, confirm: str, parsed_state
    ) -> ReleaseWebhookHttpResponse:
        # The state is minted by whoever ran the Discord command, so show whose account gets the
        # pledge and make the patron click once more before anything is linked.
        member = await self._resolve_member(parsed_state.guild_id, parsed_state.discord_user_id)
        card = _discord_card(member, parsed_state.discord_user_id)
        body = "Click below to link your Patreon membership to the Discord account above"
        if parsed_state.action == "beta_access":
            body += f" and link your Minecraft username <b>{html.escape(parsed_state.minecraft_username or '')}</b>"
        return patron_page.page_response(
            title="Is this you?",
            body_html=body + ".",
            steps=(True, False, False) if parsed_state.action == "beta_access" else None,
            card=card,
            actions=patron_page.form_button(
                f"Link to {card.username}", {"code": code, "state": state, "confirm": confirm}
            ),
        )

    async def _complete_patreon_oauth_callback(
        self,
        code: str,
        parsed_state,
    ) -> ReleaseWebhookHttpResponse:
        settings = self.bot.settings
        client = PatreonOAuthClient(
            client_id=settings.patreon_oauth_client_id,
            client_secret=settings.patreon_oauth_client_secret,
            redirect_uri=settings.patreon_oauth_redirect_uri,
            campaign_id=settings.PATREON_CAMPAIGN_ID,
        )
        try:
            identity = await client.fetch_identity_for_code(code)
        except Exception as error:
            log.warning("Patreon OAuth identity fetch failed: %s", error)
            return self._html_response("Patreon authorization failed. Please try again from Discord.", status=502)
        active = is_active_entitled_patron(
            identity.status,
            eligible_tier_ids=_eligible_tier_ids(settings),
        )
        member = await self._resolve_member(parsed_state.guild_id, parsed_state.discord_user_id)
        discord_username = str(member) if member is not None else str(parsed_state.discord_user_id)
        link = PatreonLink(
            discord_user_id=parsed_state.discord_user_id,
            discord_username=discord_username,
            patreon_user_id=identity.status.patreon_user_id,
            patreon_member_id=identity.status.member_id,
            patreon_full_name=identity.status.full_name,
            patron_status=identity.status.patron_status,
            tier_ids=identity.status.tier_ids,
            last_charge_date=identity.status.last_charge_date,
            entitlement_active=active,
        )
        await upsert_patreon_link(link)
        await self._log_staff_info(
            f"Patreon link updated for <@{parsed_state.discord_user_id}>: status `{identity.status.patron_status}`, tiers `{', '.join(identity.status.tier_ids) or 'none'}`."
        )
        if not active:
            return patron_page.page_response(
                title="Almost there",
                tone="aqua",
                body_html="Your Patreon has been linked, but we haven't found that you are a <b>Contributor</b> or <b>Benefactor</b>.",
                steps=(True, True, False) if parsed_state.action == "beta_access" else None,
                actions=patron_page.button("Become a patron", patron_page.PATREON_URL, new_tab=True),
                note_html='Have you signed in with the correct account? If not, <a href="#switch">click here</a>.',
                switch_url=self._build_patreon_oauth_url(
                    member,
                    action=parsed_state.action,
                    minecraft_username=parsed_state.minecraft_username,
                ) if member is not None else None,
            )
        if parsed_state.action == "beta_access":
            minecraft_username = parsed_state.minecraft_username
            if not MC_NAME_RE.match(minecraft_username or ""):
                return self._html_response(
                    "Patreon linked, but the Minecraft username in this verification request was invalid. Please try again from Minecraft.",
                    status=400,
                )
            if member is None:
                return self._html_response(
                    "Patreon linked, but I could not find you in the DragonMineZ Discord server.",
                    status=403,
                )

            destination = BrowserFlowDestination()
            await self.start_whitelist_flow_for_user(
                member,
                destination,
                minecraft_username,
                ephemeral=False,
                active_link=link,
            )
            if destination.whitelisted:
                return self._whitelisted_page(destination.whitelisted, patreon_just_linked=True)
            message = destination.messages[-1] if destination.messages else "Verification request accepted."
            return self._html_response(message)
        return patron_page.page_response(
            title="Patreon linked",
            tone="green",
            body_html="Your Patreon is linked to your Discord account. You can close this tab and go back to Discord.",
        )

    async def _resolve_member(self, guild_id: int, user_id: int) -> discord.Member | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except Exception:
            return None

    def _html_response(
        self,
        message: str,
        *,
        status: int = 200,
        title: str | None = None,
        extra_html: str = "",
    ) -> ReleaseWebhookHttpResponse:
        failed = status >= 400
        return patron_page.page_response(
            status=status,
            title=title or ("Something went wrong" if failed else "DragonMineZ Beta Access"),
            tone="red" if failed else "gold",
            body_html=self._linkify_message(message),
            actions=extra_html,
        )

    def _expired_page(self) -> ReleaseWebhookHttpResponse:
        return self._html_response(
            "This authorization timed out or was already used. Start again from Discord.",
            status=403,
            title="Link expired",
        )

    def _whitelisted_page(self, nickname: str, *, patreon_just_linked: bool) -> ReleaseWebhookHttpResponse:
        prefix = "Patreon linked and " if patreon_just_linked else ""
        downloads = patron_page.link("download", patron_page.DOWNLOADS_CHANNEL_URL)
        return patron_page.page_response(
            title="You're in!",
            tone="green",
            body_html=(
                f"{prefix}<b>{html.escape(nickname)}</b> has been whitelisted. "
                f"You now have access to {downloads} DragonMineZ early-access versions!"
            ),
            steps=(True, True, True),
        )

    def _linkify_message(self, message: str) -> str:
        parts: list[str] = []
        last = 0
        for match in URL_RE.finditer(message):
            parts.append(html.escape(message[last:match.start()]))
            url = match.group(0).rstrip(".,)")
            trailing = match.group(0)[len(url):]
            safe_url = html.escape(url, quote=True)
            parts.append(f'<a href="{safe_url}">{html.escape(url)}</a>')
            parts.append(html.escape(trailing))
            last = match.end()
        parts.append(html.escape(message[last:]))
        return re.sub(r"`([^`<>]+)`", r"<b>\1</b>", "".join(parts))

    async def _handle_patreon_webhook(self, body: bytes, headers) -> ReleaseWebhookHttpResponse:
        settings = self.bot.settings
        if not verify_patreon_webhook_signature(body, headers, settings.patreon_webhook_secret):
            return text_http_response(403, "Forbidden")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return text_http_response(400, "Invalid JSON body")
        member_id = str(((payload.get("data") or {}).get("id") or "")).strip()
        if not member_id:
            return text_http_response(400, "Missing Patreon member id")
        link = await get_patreon_link_by_member_id(member_id)
        if link is None:
            log.debug("Patreon webhook for unlinked member %s ignored", member_id)  # free/unlinked members: nothing to act on
            return text_http_response(202, "Patreon webhook accepted")
        if not settings.PATREON_CREATOR_TOKEN:
            return text_http_response(500, "Patreon creator token is not configured")

        client = PatreonCreatorClient(
            creator_token=settings.PATREON_CREATOR_TOKEN,
            campaign_id=settings.PATREON_CAMPAIGN_ID,
        )
        try:
            status = await client.fetch_member_status(member_id)
        except Exception as error:
            # A deleted pledge often 404s on /members/{id}; the webhook body carries the same member resource.
            log.warning("Patreon member lookup failed for %s, using webhook payload: %s", member_id, error)
            status = parse_member_resource_status(payload, campaign_id=settings.PATREON_CAMPAIGN_ID)
        active = is_active_entitled_patron(status, eligible_tier_ids=_eligible_tier_ids(settings))
        await update_link_entitlement(
            discord_user_id=link.discord_user_id,
            patron_status=status.patron_status,
            tier_ids=status.tier_ids,
            last_charge_date=status.last_charge_date,
            entitlement_active=active,
        )
        if active:
            await self._log_staff_info(
                f"Patreon webhook kept <@{link.discord_user_id}> active: status `{status.patron_status}`, tiers `{', '.join(status.tier_ids) or 'none'}`."
            )
            return text_http_response(202, "Patreon webhook accepted")

        try:
            await self.revoke_owner_access(link.discord_user_id, status.patron_status)
        except Exception:
            # Patreon pauses the whole webhook after repeated failures, so never 500 here: the grants stay
            # active (whitelist goes first) and the hourly sync retries the removal.
            log.exception("Patreon webhook revoke failed for %s; hourly sync will retry", link.discord_user_id)
        return text_http_response(202, "Patreon webhook accepted")

    async def _remove_whitelist_grants(
        self,
        owner_discord_user_id: int,
        grants: list[PatreonGrant],
        patron_status: str | None,
    ) -> None:
        nicknames = sorted({grant.minecraft_username for grant in grants if grant.minecraft_username})
        if not nicknames:
            await self._log_staff_info(
                f"Patreon access expired for <@{owner_discord_user_id}> with no active whitelist grants to remove."
            )
            return
        branch = _patreon_remove_branch_name(owner_discord_user_id)
        keys = {nickname.casefold() for nickname in nicknames}
        base_text, _base_sha = await self.gh.get_whitelist_file(ref=self.gh.base_branch)
        base_lines = [ln.strip() for ln in base_text.splitlines() if ln.strip()]
        keys -= _manual_whitelist_keys(base_lines)
        remaining = [line for line in base_lines if line.casefold() not in keys]
        if remaining == base_lines:
            await self._log_staff_info(
                f"Patreon access expired for <@{owner_discord_user_id}>; no matching whitelist lines found for `{', '.join(nicknames)}`."
            )
            return
        await self.gh.create_branch(branch, self.gh.base_branch)
        # A failed earlier attempt leaves this branch behind; start it fresh from the base.
        await self.gh.reset_branch(branch, await self.gh.get_ref_sha(self.gh.base_branch))
        branch_text, branch_sha = await self.gh.get_whitelist_file(ref=branch)
        branch_lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
        updated = [line for line in branch_lines if line.casefold() not in keys]
        await self.gh.put_whitelist_file(
            branch=branch,
            new_text=("\n".join(updated) + "\n") if updated else "",
            sha=branch_sha,
            message=f"Remove expired Patreon beta access for {owner_discord_user_id}",
        )
        pr_data = await self.gh.create_or_get_pr(
            head_branch=branch,
            title=f"Remove expired Patreon beta access for {owner_discord_user_id}",
            body=(
                f"Patreon status `{patron_status}` is no longer active. "
                f"Removing whitelist entries: {', '.join(nicknames)}."
            ),
        )
        await self.gh.merge_pr(pr_data["number"])
        await self.gh.add_pr_comment(
            pr_data["number"],
            f"Automatically removed expired Patreon whitelist entries: {', '.join(nicknames)}.",
        )
        await self.gh.remove_branch(branch)
        await self._log_staff_info(
            f"Patreon access expired for <@{owner_discord_user_id}>; removed `{', '.join(nicknames)}`.\n-# [GitHub PR](<{pr_data['html_url']}>)"
        )

def setup(bot: discord.Bot):
    bot.add_cog(PatreonWhitelistFlowCog(bot))
