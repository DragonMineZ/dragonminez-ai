import logging

import discord
from discord.ext import commands

from bulmaai.github.github_app_auth import GitHubAppAuth
from bulmaai.github.github_service import GitHubService
from bulmaai.ui.patreon_views import UserConfirmView, AdminPRView, MC_NAME_RE
from bulmaai.utils.permissions import has_patreon_access_role, is_admin

log = logging.getLogger(__name__)

ADMIN_PING_ROLE_ID = 1309022450671161476
STAFF_CHANNEL_ID = 1493390527004147876


def _patreon_branch_name(user_id: int) -> str:
    return f"patreon/user-{user_id}"


async def _edit_user_status_message(message, content: str) -> None:
    if message is None:
        return
    try:
        await message.edit(content=content, view=None)
    except Exception:
        log.exception("Failed to edit Patreon whitelist user status message")


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
    """Patreon beta whitelist workflow used by the AI tool."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.gh = self._build_github_service()

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

    async def start_whitelist_flow_for_user(
        self,
        member: discord.Member,
        channel: discord.abc.Messageable,
        initial_nickname: str | None = None,
    ) -> str:
        """
        Core Patreon whitelist workflow used by the OpenAI tool (and any future commands).

        Returns one of:
        - 'user_not_allowed'
        - 'invalid_nickname'
        - 'asked_for_nickname'
        - 'flow_started'
        """
        if not is_admin(member) and not has_patreon_access_role(
            member,
            settings=self.bot.settings,
        ):
            await channel.send(
                f"{member.mention} You don't have permission to request Patreon whitelist access. "
                f"You need the Patreon Contributor role in Discord. Maybe try [this](https://support.patreon.com/hc/en-us/articles/212052266-Getting-Discord-access) first."
            )
            return "user_not_allowed"

        async def run_flow_with_nick(nickname: str) -> str:
            if not MC_NAME_RE.match(nickname):
                await channel.send(
                    f"{member.mention} Nickname must be 3-16 chars, letters/numbers/_ only."
                )
                return "invalid_nickname"

            async def on_user_confirm(
                interaction: discord.Interaction,
                initial_nick: str,
            ):
                try:
                    await self._submit_whitelist_request(
                        interaction=interaction,
                        initial_nick=initial_nick,
                    )
                except Exception:
                    log.exception(
                        "Failed to create Patreon whitelist request",
                        extra={
                            "event": "patreon_whitelist_request_failed",
                            "user_id": interaction.user.id,
                            "whitelist_repo": self.gh.repo,
                            "whitelist_file_path": self.gh.whitelist_file_path,
                        },
                    )
                    await interaction.followup.send(
                        "I could not submit the whitelist request because the GitHub update failed. "
                        "Please ask staff to check the bot logs.",
                        ephemeral=True,
                    )

            await channel.send(
                f"You've said that `{nickname}` is your Minecraft nickname to get access to "
                f"the Patreon-only releases, is this correct?",
                view=UserConfirmView(
                    requester_id=member.id,
                    nickname=nickname,
                    on_confirm=on_user_confirm,
                ),
            )
            return "flow_started"

        if initial_nickname:
            return await run_flow_with_nick(initial_nickname.strip())

        await channel.send(
            f"{member.mention} Please reply with your Minecraft nickname "
            f"so we can start the Patreon beta whitelist process."
        )
        return "asked_for_nickname"

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
            await interaction.followup.send(
                f"`{state['nick']}` is already whitelisted. Nothing to do.",
                ephemeral=True,
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
            await interaction.followup.send(
                "I created the whitelist PR, but I could not notify staff. "
                "Please ask staff to check the bot logs.",
                ephemeral=True,
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

        await interaction.followup.send(
            "Request submitted. Please wait for an administrator to approve.",
            ephemeral=True,
        )


def setup(bot: discord.Bot):
    bot.add_cog(PatreonWhitelistFlowCog(bot))

