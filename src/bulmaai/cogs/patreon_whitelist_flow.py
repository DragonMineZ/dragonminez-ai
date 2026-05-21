import logging
import time
import json
import asyncio
import html
import re
from urllib.parse import urlparse

import discord
from discord.ext import commands

from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.services.discord_oauth import (
    DiscordOAuthClient,
    build_discord_authorization_url,
    build_discord_oauth_state,
    parse_discord_oauth_state,
)
from bulmaai.services.patreon_access import (
    PatreonCreatorClient,
    PatreonOAuthClient,
    build_patreon_authorization_url,
    build_patreon_oauth_state,
    is_active_entitled_patron,
    parse_patreon_oauth_state,
    verify_patreon_webhook_signature,
)
from bulmaai.services.patreon_grants import (
    PatreonGrant,
    PatreonGrantKind,
    PatreonLink,
    count_active_gifts_for_owner,
    deactivate_grants_for_owner,
    get_patreon_link,
    get_patreon_link_by_member_id,
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
from bulmaai.ui.patreon_views import AdminPRView, MC_NAME_RE
from bulmaai.utils.permissions import has_patreon_access_role

log = logging.getLogger(__name__)

ADMIN_PING_ROLE_ID = 1309022450671161476
STAFF_CHANNEL_ID = 1493390527004147876
CONTRIBUTOR_ROLE_ID = 1287877272224665640
BENEFACTOR_ROLE_ID = 1287877305259130900
PATREON_OAUTH_TTL_SECONDS = 10 * 60
PATREON_WEBHOOK_PATH = "/patreon/webhook"
BETA_ACCESS_ROUTE_PREFIX = "/beta-access/"
BETA_ACCESS_START_PATH = "/beta-access/start"
DISCORD_OAUTH_TTL_SECONDS = 10 * 60
URL_RE = re.compile(r"https?://[^\s<]+")


def _patreon_branch_name(user_id: int) -> str:
    return f"patreon/user-{user_id}"


def _patreon_gift_branch_name(owner_id: int, recipient_id: int) -> str:
    return f"patreon/gift-{owner_id}-{recipient_id}"


def _patreon_remove_branch_name(owner_id: int) -> str:
    return f"patreon/remove-{owner_id}"


def _eligible_tier_ids(settings) -> tuple[str, ...]:
    return tuple(str(tier_id) for tier_id in settings.patreon_eligible_tier_ids)


def _gift_limit_for_member(member: discord.Member) -> int:
    role_ids = {role.id for role in getattr(member, "roles", [])}
    if BENEFACTOR_ROLE_ID in role_ids:
        return 2
    if CONTRIBUTOR_ROLE_ID in role_ids:
        return 1
    return 0


def _is_active_link(link: PatreonLink, settings) -> bool:
    return link.entitlement_active


async def _edit_user_status_message(message, content: str) -> None:
    if message is None:
        return
    try:
        await message.edit(content=content, view=None)
    except Exception:
        log.exception("Failed to edit Patreon whitelist user status message")


async def _edit_user_interaction_status(interaction: discord.Interaction, content: str) -> None:
    edit_original_response = getattr(interaction, "edit_original_response", None)
    if edit_original_response is not None:
        try:
            await edit_original_response(content=content, view=None)
            return
        except Exception:
            log.exception("Failed to edit Patreon whitelist interaction response")

    message = getattr(interaction, "message", None)
    if message is not None:
        await _edit_user_status_message(message, content)
        return

    followup = getattr(interaction, "followup", None)
    if followup is not None:
        await followup.send(content, ephemeral=True)


async def _dm_user(user, content: str) -> None:
    try:
        await user.send(content)
    except Exception:
        log.exception(
            "Failed to DM Patreon whitelist requester",
            extra={
                "event": "patreon_whitelist_dm_failed",
                "user_id": getattr(user, "id", None),
            },
        )


async def _send_message(destination, content: str, *, ephemeral: bool = False, **kwargs) -> None:
    if ephemeral:
        kwargs["ephemeral"] = True
    await destination.send(content, **kwargs)


class BrowserFlowDestination:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str, **kwargs) -> None:
        self.messages.append(str(content))


async def _pick_staff_channel(
    bot: discord.Bot,
    ctx_or_inter: discord.Interaction | discord.ApplicationContext | None = None,
) -> discord.abc.Messageable | None:
    if STAFF_CHANNEL_ID:
        channel = bot.get_channel(STAFF_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(STAFF_CHANNEL_ID)
            except Exception:
                log.exception("Failed to fetch Patreon staff log channel %s", STAFF_CHANNEL_ID)
                channel = None
        if channel is not None and hasattr(channel, "send"):
            return channel

    if ctx_or_inter is not None and not isinstance(ctx_or_inter.channel, discord.DMChannel):
        return ctx_or_inter.channel

    return None


class PatreonWhitelistFlowCog(commands.Cog):
    """Patreon beta whitelist workflow used by the /beta-access command."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.gh = self._build_github_service()
        self._patreon_routes_registered = False

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

    @discord.slash_command(
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

    @discord.slash_command(name="link-patreon", description="Link your Patreon account")
    async def link_patreon(self, ctx: discord.ApplicationContext) -> None:
        await self._handle_link_patreon_command(ctx)

    @discord.slash_command(name="gift-beta")
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

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._register_patreon_routes()

    def cog_unload(self) -> None:
        path = urlparse(self.bot.settings.patreon_oauth_redirect_uri).path
        unregister_extra_get_route(path)
        unregister_extra_get_route(BETA_ACCESS_ROUTE_PREFIX)
        unregister_extra_raw_webhook_route(PATREON_WEBHOOK_PATH)

    def _register_patreon_routes(self) -> None:
        if self._patreon_routes_registered:
            return
        self._patreon_routes_registered = True

        loop = asyncio.get_running_loop()
        callback_path = urlparse(self.bot.settings.patreon_oauth_redirect_uri).path
        discord_callback_path = urlparse(self.bot.settings.discord_oauth_redirect_uri).path

        def handle_oauth_callback(path: str, query: dict[str, list[str]]) -> ReleaseWebhookHttpResponse:
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not code or not state:
                return text_http_response(400, "Missing OAuth code or state")
            future = asyncio.run_coroutine_threadsafe(
                self._handle_patreon_oauth_callback(code, state),
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

        def handle_beta_access(path: str, query: dict[str, list[str]]) -> ReleaseWebhookHttpResponse:
            if path == BETA_ACCESS_START_PATH:
                return self._handle_beta_access_start(query)
            if path != discord_callback_path:
                return text_http_response(404, "Not found")

            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not code or not state:
                return self._html_response("Missing Discord OAuth code or state.", status=400)
            future = asyncio.run_coroutine_threadsafe(
                self._handle_beta_access_discord_callback(code=code, state=state),
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

        url = self._build_discord_oauth_url(username)
        if url is None:
            return self._html_response(
                "Discord verification is not configured yet. Ask staff to check the bot settings.",
                status=500,
            )

        return ReleaseWebhookHttpResponse(
            status=302,
            body=b"",
            headers=(("Location", url),),
        )

    def _build_discord_oauth_url(self, minecraft_username: str) -> str | None:
        settings = self.bot.settings
        if not settings.discord_oauth_client_id or not settings.discord_oauth_client_secret:
            return None
        state = build_discord_oauth_state(
            secret=settings.discord_oauth_client_secret,
            minecraft_username=minecraft_username,
            expires_at=int(time.time() + DISCORD_OAUTH_TTL_SECONDS),
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

        try:
            discord_user_id = await DiscordOAuthClient(
                client_id=settings.discord_oauth_client_id,
                client_secret=settings.discord_oauth_client_secret,
                redirect_uri=settings.discord_oauth_redirect_uri,
            ).fetch_user_id_for_code(code)
        except Exception:
            log.exception("Discord OAuth identity fetch failed")
            return self._html_response("Discord authorization failed. Please try again.", status=500)

        member = await self._resolve_member_across_guilds(discord_user_id)
        if member is None:
            return self._html_response(
                "Join the DragonMineZ Discord server before verifying beta access.",
                status=403,
            )

        destination = BrowserFlowDestination()
        await self.start_whitelist_flow_for_user(
            member,
            destination,
            parsed_state.minecraft_username,
            ephemeral=False,
        )
        message = destination.messages[-1] if destination.messages else "Verification request accepted."
        return self._html_response(message)

    async def _resolve_member_across_guilds(self, user_id: int) -> discord.Member | None:
        guilds = list(getattr(self.bot, "guilds", []) or [])
        for guild in guilds:
            member = guild.get_member(user_id)
            if member is not None:
                return member
        for guild in guilds:
            try:
                return await guild.fetch_member(user_id)
            except Exception:
                continue
        return None

    async def _handle_beta_access_command(
        self,
        ctx: discord.ApplicationContext,
        username: str,
    ) -> None:
        await ctx.defer(ephemeral=True)

        if not isinstance(ctx.author, discord.Member):
            await ctx.followup.send(
                "Use `/beta-access` inside the DragonMineZ server so I can verify your Patreon role.",
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
                "Use `/link-patreon` inside the DragonMineZ server.",
                ephemeral=True,
            )
            return
        await self._send_patreon_oauth_prompt(ctx.author, ctx.followup, ephemeral=True)

    def _build_patreon_oauth_url(self, member: discord.Member, *, action: str = "link") -> str | None:
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
    ) -> None:
        url = self._build_patreon_oauth_url(member)
        if url is None:
            await _send_message(
                destination,
                "Patreon linking is not configured yet. Ask staff to check the bot settings.",
                ephemeral=ephemeral,
            )
            return
        await _send_message(
            destination,
            f"Authorize with Patreon to link your account, then run the command again: {url}",
            ephemeral=ephemeral,
        )

    async def start_whitelist_flow_for_user(
        self,
        member: discord.Member,
        destination,
        initial_nickname: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        """
        Core Patreon whitelist workflow used by /beta-access.
        """
        nickname = initial_nickname.strip() if initial_nickname is not None else ""
        if not MC_NAME_RE.match(nickname):
            await _send_message(
                destination,
                "Invalid Minecraft username. Use 3-16 letters, numbers, or underscores.",
                ephemeral=ephemeral,
            )
            return

        if not has_patreon_access_role(member, settings=self.bot.settings):
            await _send_message(
                destination,
                "You need a Patreon beta access role to use `/beta-access`.",
                ephemeral=ephemeral,
            )
            return

        link = await get_patreon_link(member.id)
        if link is None or not _is_active_link(link, self.bot.settings):
            await self._send_patreon_oauth_prompt(member, destination, ephemeral=ephemeral)
            return

        try:
            pr_url = await self._auto_approve_beta_access(member, nickname)
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
        await _send_message(
            destination,
            f"`{nickname}` was approved automatically for Patreon beta access.",
            ephemeral=ephemeral,
        )
        await self._log_staff_info(
            f"{member.mention} linked Patreon access and `{nickname}` was approved automatically.\nPR: {pr_url}"
        )

    async def _auto_approve_beta_access(self, member: discord.Member, nickname: str) -> str | None:
        branch = _patreon_branch_name(member.id)
        pr_data = await self._create_whitelist_add_pr(
            branch=branch,
            nickname=nickname,
            title=f"Add beta tester: {nickname}",
            commit_message=f"Add beta tester: {nickname}",
            body=f"Automatically approved through Patreon OAuth for Discord user {member} ({member.id}).",
        )
        if pr_data is None:
            return None
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]
        await self.gh.merge_pr(pr_number)
        await self.gh.add_pr_comment(
            pr_number,
            f"Automatically approved through Patreon OAuth for {member} ({member.id}).",
        )
        await self.gh.remove_branch(branch)
        return pr_url

    async def _create_whitelist_add_pr(
        self,
        *,
        branch: str,
        nickname: str,
        title: str,
        commit_message: str,
        body: str,
    ) -> dict | None:
        base_text, _base_sha = await self.gh.get_whitelist_file(ref=self.gh.base_branch)
        base_lines = [ln.strip() for ln in base_text.splitlines() if ln.strip()]
        if nickname in base_lines:
            return None

        await self.gh.create_branch(branch, self.gh.base_branch)
        branch_text, branch_sha = await self.gh.get_whitelist_file(ref=branch)
        branch_lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
        if nickname not in branch_lines:
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

    async def _handle_gift_beta_command(
        self,
        ctx: discord.ApplicationContext,
        recipient: discord.Member,
        username: str,
    ) -> None:
        await ctx.defer(ephemeral=True)
        if not isinstance(ctx.author, discord.Member):
            await ctx.followup.send(
                "Use `/gift-beta` inside the DragonMineZ server.",
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

        gift_limit = _gift_limit_for_member(ctx.author)
        used_gifts = await count_active_gifts_for_owner(ctx.author.id)
        if used_gifts >= gift_limit:
            await ctx.followup.send(
                f"You have already used your active Patreon gift limit ({gift_limit}).",
                ephemeral=True,
            )
            return

        try:
            await self._submit_gift_request(ctx.author, recipient, nickname)
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

        await ctx.followup.send(
            f"Gift submitted for staff approval: {recipient.mention} as `{nickname}`.",
            ephemeral=True,
        )

    async def _submit_gift_request(
        self,
        owner: discord.Member,
        recipient: discord.Member,
        nickname: str,
    ) -> None:
        branch = _patreon_gift_branch_name(owner.id, recipient.id)
        pr_data = await self._create_whitelist_add_pr(
            branch=branch,
            nickname=nickname,
            title=f"Gift beta tester: {nickname}",
            commit_message=f"Gift beta tester: {nickname}",
            body=(
                f"Gift requested by Discord user {owner} ({owner.id}) "
                f"for {recipient} ({recipient.id})."
            ),
        )
        if pr_data is None:
            await self._log_staff_info(
                f"{owner.mention} tried to gift beta access to `{nickname}`, but that username is already whitelisted."
            )
            return
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]
        staff_channel = await _pick_staff_channel(self.bot)
        if staff_channel is None:
            raise RuntimeError("Patreon staff channel unavailable")

        admin_view: AdminPRView | None = None

        async def admin_confirm(admin_inter: discord.Interaction):
            await self.gh.merge_pr(pr_number)
            await self.gh.add_pr_comment(
                pr_number,
                f"Gift approved by {admin_inter.user}, PR merged.",
            )
            await self.gh.remove_branch(branch)
            await upsert_whitelist_grant(
                PatreonGrant(
                    owner_discord_user_id=owner.id,
                    beneficiary_discord_user_id=recipient.id,
                    beneficiary_discord_username=str(recipient),
                    minecraft_username=nickname,
                    kind=PatreonGrantKind.GIFT,
                    active=True,
                    source_pr_url=pr_url,
                )
            )
            await admin_inter.followup.send(
                f"PR #{pr_number} merged. `{nickname}` approved as a Patreon gift."
            )

        async def admin_edit(admin_inter: discord.Interaction, new_nick: str):
            nonlocal nickname
            old_nick = nickname
            branch_text, branch_sha = await self.gh.get_whitelist_file(ref=branch)
            lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
            lines = [ln for ln in lines if ln != old_nick]
            if new_nick not in lines:
                lines.append(new_nick)
            await self.gh.put_whitelist_file(
                branch=branch,
                new_text="\n".join(lines) + "\n",
                sha=branch_sha,
                message=f"Update gifted beta tester: {old_nick} -> {new_nick}",
            )
            nickname = new_nick
            if admin_view is not None:
                admin_view.nickname = new_nick
            await admin_inter.followup.send(f"Updated PR branch nickname to `{new_nick}`.")

        async def admin_reject(admin_inter: discord.Interaction):
            await self.gh.close_pr(pr_number)
            await self.gh.add_pr_comment(
                pr_number,
                f"Gift rejected by {admin_inter.user}, PR closed.",
            )
            await self.gh.remove_branch(branch)
            await admin_inter.followup.send(f"PR #{pr_number} closed. Gift rejected.")

        admin_view = AdminPRView(
            pr_number=pr_number,
            nickname=nickname,
            branch=branch,
            on_confirm=admin_confirm,
            on_edit=admin_edit,
            on_reject=admin_reject,
        )

        await staff_channel.send(
            f"<@&{ADMIN_PING_ROLE_ID}>\n\n"
            f"{owner.mention} wants to gift Patreon beta access to {recipient.mention} as `{nickname}`.\n"
            f"PR: {pr_url}",
            view=admin_view,
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )

    async def _log_staff_info(self, content: str) -> None:
        channel = await _pick_staff_channel(self.bot)
        if channel is None:
            log.warning("Patreon staff log channel unavailable: %s", content)
            return
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())

    async def _handle_patreon_oauth_callback(self, code: str, state: str) -> ReleaseWebhookHttpResponse:
        settings = self.bot.settings
        if not settings.patreon_oauth_client_id or not settings.patreon_oauth_client_secret:
            return text_http_response(500, "Patreon OAuth is not configured")
        parsed_state = parse_patreon_oauth_state(
            settings.patreon_oauth_client_secret,
            state,
            now=time.time,
        )
        if parsed_state is None:
            return text_http_response(403, "Patreon authorization expired")

        client = PatreonOAuthClient(
            client_id=settings.patreon_oauth_client_id,
            client_secret=settings.patreon_oauth_client_secret,
            redirect_uri=settings.patreon_oauth_redirect_uri,
            campaign_id=settings.PATREON_CAMPAIGN_ID,
        )
        identity = await client.fetch_identity_for_code(code)
        active = is_active_entitled_patron(
            identity.status,
            eligible_tier_ids=_eligible_tier_ids(settings),
        )
        member = await self._resolve_member(parsed_state.guild_id, parsed_state.discord_user_id)
        discord_username = str(member) if member is not None else str(parsed_state.discord_user_id)
        await upsert_patreon_link(
            PatreonLink(
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
        )
        await self._log_staff_info(
            f"Patreon link updated for <@{parsed_state.discord_user_id}>: status `{identity.status.patron_status}`, tiers `{', '.join(identity.status.tier_ids) or 'none'}`."
        )
        if not active:
            return self._html_response("Patreon linked, but active Contributor/Benefactor access was not found.")
        return self._html_response("Patreon linked. You can close this tab and return to Discord.")

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
        title: str = "DragonMineZ Beta Access",
    ) -> ReleaseWebhookHttpResponse:
        safe_message = self._linkify_message(message)
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title></head><body>"
            f"<main><h1>{html.escape(title)}</h1><p>{safe_message}</p></main>"
            "</body></html>"
        )
        return ReleaseWebhookHttpResponse(
            status=status,
            body=body.encode("utf-8"),
            content_type="text/html; charset=utf-8",
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
        return "".join(parts)

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
            await self._log_staff_info(f"Patreon webhook received for unlinked member `{member_id}`.")
            return text_http_response(202, "Patreon webhook accepted")
        if not settings.PATREON_CREATOR_TOKEN:
            return text_http_response(500, "Patreon creator token is not configured")

        client = PatreonCreatorClient(
            creator_token=settings.PATREON_CREATOR_TOKEN,
            campaign_id=settings.PATREON_CAMPAIGN_ID,
        )
        status = await client.fetch_member_status(member_id)
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

        grants = await deactivate_grants_for_owner(link.discord_user_id)
        await self._remove_whitelist_grants(link.discord_user_id, grants, status.patron_status)
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
        base_text, _base_sha = await self.gh.get_whitelist_file(ref=self.gh.base_branch)
        base_lines = [ln.strip() for ln in base_text.splitlines() if ln.strip()]
        remaining = [line for line in base_lines if line not in set(nicknames)]
        if remaining == base_lines:
            await self._log_staff_info(
                f"Patreon access expired for <@{owner_discord_user_id}>; no matching whitelist lines found for `{', '.join(nicknames)}`."
            )
            return
        await self.gh.create_branch(branch, self.gh.base_branch)
        branch_text, branch_sha = await self.gh.get_whitelist_file(ref=branch)
        branch_lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
        updated = [line for line in branch_lines if line not in set(nicknames)]
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
            f"Patreon access expired for <@{owner_discord_user_id}>; removed `{', '.join(nicknames)}`.\nPR: {pr_data['html_url']}"
        )

    async def _submit_whitelist_request(
        self,
        *,
        interaction: discord.Interaction,
        initial_nick: str,
    ) -> None:
        state = {"nick": initial_nick}
        branch = _patreon_branch_name(interaction.user.id)
        requester = interaction.user
        user_status_message = getattr(interaction, "message", None)

        base_text, _base_sha = await self.gh.get_whitelist_file(ref=self.gh.base_branch)
        base_lines = [ln.strip() for ln in base_text.splitlines() if ln.strip()]

        if state["nick"] in base_lines:
            await _edit_user_interaction_status(
                interaction,
                f"`{state['nick']}` is already whitelisted. Nothing to do.",
            )
            return

        await self.gh.create_branch(branch, self.gh.base_branch)

        branch_text, branch_sha = await self.gh.get_whitelist_file(ref=branch)
        branch_lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
        if state["nick"] in branch_lines:
            log.info(
                "Patreon whitelist branch already contains nickname; reusing PR flow",
                extra={
                    "event": "patreon_whitelist_branch_already_updated",
                    "user_id": interaction.user.id,
                    "branch": branch,
                    "nickname": state["nick"],
                },
            )
        else:
            branch_lines.append(state["nick"])
            new_text = "\n".join(branch_lines) + "\n"
            await self.gh.put_whitelist_file(
                branch=branch,
                new_text=new_text,
                sha=branch_sha,
                message=f"Add beta tester: {state['nick']}",
            )

        pr_data = await self.gh.create_or_get_pr(
            head_branch=branch,
            title=f"Add beta tester: {state['nick']}",
            body=f"Requested by Discord user {interaction.user} ({interaction.user.id}).",
        )
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]

        staff_channel = await _pick_staff_channel(self.bot, interaction)
        if staff_channel is None:
            await _edit_user_interaction_status(
                interaction,
                "I created the whitelist PR, but I could not notify staff. "
                "Please ask staff to check the bot logs.",
            )
            log.error(
                "Patreon whitelist PR created but staff channel unavailable",
                extra={
                    "event": "patreon_whitelist_staff_channel_missing",
                    "user_id": interaction.user.id,
                    "pr_number": pr_number,
                },
            )
            return
        staff_guild = getattr(staff_channel, "guild", None)
        admin_role = staff_guild.get_role(ADMIN_PING_ROLE_ID) if staff_guild else None
        mention = admin_role.mention if admin_role else f"<@&{ADMIN_PING_ROLE_ID}>"

        admin_view: AdminPRView | None = None

        async def admin_confirm(admin_inter: discord.Interaction):
            await self.gh.merge_pr(pr_number)
            await self.gh.add_pr_comment(
                pr_number,
                f"Request approved by {admin_inter.user}, PR merged.",
            )
            await self.gh.remove_branch(branch)
            await _edit_user_status_message(user_status_message, "Success")
            await _dm_user(
                requester,
                f"Congratulations, {admin_inter.user} has approved your request and you now have access to the latest previews!",
            )
            await admin_inter.followup.send(
                f"PR #{pr_number} merged. `{state['nick']}` approved."
            )

        async def admin_edit(admin_inter: discord.Interaction, new_nick: str):
            old_nick = state["nick"]

            branch_text, branch_sha = await self.gh.get_whitelist_file(ref=branch)
            lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
            lines = [ln for ln in lines if ln != old_nick]
            if new_nick not in lines:
                lines.append(new_nick)

            updated = "\n".join(lines) + "\n"
            await self.gh.put_whitelist_file(
                branch=branch,
                new_text=updated,
                sha=branch_sha,
                message=f"Update beta tester: {old_nick} -> {new_nick}",
            )

            state["nick"] = new_nick
            if admin_view is not None:
                admin_view.nickname = new_nick

            await admin_inter.followup.send(
                f"Updated PR branch nickname to `{new_nick}`."
            )

        async def admin_reject(admin_inter: discord.Interaction):
            await self.gh.close_pr(pr_number)
            await self.gh.add_pr_comment(
                pr_number,
                f"Request rejected by {admin_inter.user}, PR closed.",
            )
            await self.gh.remove_branch(branch)
            await _edit_user_status_message(user_status_message, "Rejected")
            await _dm_user(
                requester,
                f"Your Patreon whitelist request was rejected by {admin_inter.user}. Please contact staff if you think this was a mistake.",
            )
            await admin_inter.followup.send(
                f"PR #{pr_number} closed. Request rejected."
            )

        admin_view = AdminPRView(
            pr_number=pr_number,
            nickname=state["nick"],
            branch=branch,
            on_confirm=admin_confirm,
            on_edit=admin_edit,
            on_reject=admin_reject,
        )

        await staff_channel.send(
            f"{mention}\n\n"
            f"{interaction.user.mention} has set their Patreon Minecraft nickname as "
            f"`{state['nick']}`.\n"
            f"\n"
            f"PR: {pr_url}",
            view=admin_view,
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )

        await _edit_user_interaction_status(
            interaction,
            "Request submitted. Please wait for an administrator to approve.",
        )


def setup(bot: discord.Bot):
    bot.add_cog(PatreonWhitelistFlowCog(bot))
