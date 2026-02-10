import logging
import os

import discord
from discord.ext import commands

from src.bulmaai.github.github_app_auth import GitHubAppAuth
from src.bulmaai.github.github_whitelist import GitHubWhitelistService
from src.bulmaai.ui.patreon_views import UserConfirmView, AdminPRView, MC_NAME_RE
from src.bulmaai.utils.permissions import is_admin, has_any_allowed_role

log = logging.getLogger(__name__)

ALLOWED_ROLE_ID_1 = 1287877272224665640
ALLOWED_ROLE_ID_2 = 1287877305259130900
ADMIN_PING_ROLE_ID = 1309022450671161476
STAFF_CHANNEL_ID = 1470178423862460510  # Ticket 0201


def _pick_staff_channel(ctx_or_inter: discord.Interaction | discord.ApplicationContext) -> discord.abc.Messageable:
    guild = ctx_or_inter.guild
    if STAFF_CHANNEL_ID and guild:
        ch = guild.get_channel(STAFF_CHANNEL_ID)
        if ch is not None:
            return ch
    # Fallback: use the channel where the interaction happened
    return ctx_or_inter.channel  # type: ignore[return-value]


class AdminCog(commands.Cog):
    """Admin logic for Patreon whitelist flow (no public commands)."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.gh = self._build_github_service()

    def _build_github_service(self) -> GitHubWhitelistService:
        auth = GitHubAppAuth(
            app_id=self._env_required("GH_APP_ID"),
            installation_id=self._env_required("GH_INSTALLATION_ID"),
            private_key_pem=self._env_required("GH_APP_PRIVATE_KEY_PEM").replace("\\n", "\n"),
        )

        owner = self._env_default("GITHUB_OWNER", "DragonMineZ")
        repo = self._env_default("GITHUB_REPO", ".github")
        base_branch = self._env_default("GITHUB_BASE_BRANCH", "main")
        file_path = self._env_default("GITHUB_FILE_PATH", "allowed_betatesters.txt")

        return GitHubWhitelistService(
            auth=auth,
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            file_path=file_path,
        )

    @staticmethod
    def _env_required(name: str) -> str:
        v = os.getenv(name)
        if not v:
            raise RuntimeError(f"Missing env var: {name}")
        return v

    @staticmethod
    def _env_default(name: str, default: str) -> str:
        return os.getenv(name, default)

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
        # Role-gated: 2 roles allowed; administrators bypass
        if not is_admin(member) and not has_any_allowed_role(
            member, (ALLOWED_ROLE_ID_1, ALLOWED_ROLE_ID_2)
        ):
            await channel.send(
                f"{member.mention} You don’t have permission to request Patreon whitelist access."
            )
            return "user_not_allowed"

        async def run_flow_with_nick(nickname: str) -> str:
            if not MC_NAME_RE.match(nickname):
                await channel.send(
                    f"{member.mention} Nickname must be 3–16 chars, letters/numbers/_ only."
                )
                return "invalid_nickname"

            async def on_user_confirm(
                interaction: discord.Interaction,
                initial_nick: str,
            ):
                # Use mutable state so admin edit can update the nickname
                state = {"nick": initial_nick}

                safe_user = interaction.user.name.replace(" ", "_")
                branch = f"patreon/{safe_user}-{interaction.user.id}"

                # 1) Create/update PR branch with the new nickname appended
                await self.gh.create_branch(branch, self.gh.base_branch)

                base_text, base_sha = await self.gh.get_file(
                    self.gh.file_path, ref=self.gh.base_branch
                )
                base_lines = [ln.strip() for ln in base_text.splitlines() if ln.strip()]

                if state["nick"] in base_lines:
                    await interaction.followup.send(
                        f"`{state['nick']}` is already whitelisted. Nothing to do.",
                        ephemeral=True,
                    )
                    return

                new_text = "\n".join(base_lines + [state["nick"]]) + "\n"
                await self.gh.put_file(
                    branch=branch,
                    new_text=new_text,
                    sha=base_sha,
                    message=f"Add beta tester: {state['nick']}",
                )

                pr_number, pr_url = await self.gh.create_pr(
                    head_branch=branch,
                    title=f"Add beta tester: {state['nick']}",
                    body=f"Requested by Discord user {interaction.user} ({interaction.user.id}).",
                )

                # 2) Post staff approval message with admin-only buttons
                admin_role = interaction.guild.get_role(ADMIN_PING_ROLE_ID)
                mention = admin_role.mention if admin_role else f"<@&{ADMIN_PING_ROLE_ID}>"
                staff_channel = _pick_staff_channel(interaction)

                admin_view: AdminPRView | None = None

                async def admin_confirm(admin_inter: discord.Interaction):
                    await self.gh.merge_pr(pr_number)
                    await self.gh.add_comment(
                        pr_number,
                        f"Request approved by {admin_inter.user}, PR merged.",
                    )
                    await self.gh.remove_branch(pr_number)
                    await admin_inter.followup.send(
                        f"PR #{pr_number} merged. `{state['nick']}` approved."
                    )

                async def admin_edit(admin_inter: discord.Interaction, new_nick: str):
                    old_nick = state["nick"]

                    # Update the PR branch file: remove old nick, add new nick
                    branch_text, branch_sha = await self.gh.get_file(
                        self.gh.file_path, ref=branch
                    )
                    lines = [ln.strip() for ln in branch_text.splitlines() if ln.strip()]
                    lines = [ln for ln in lines if ln != old_nick]
                    if new_nick not in lines:
                        lines.append(new_nick)

                    updated = "\n".join(lines) + "\n"
                    await self.gh.put_file(
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
                    await self.gh.add_comment(
                        pr_number,
                        f"Request rejected by {admin_inter.user}, PR closed.",
                    )
                    await self.gh.remove_branch(pr_number)
                    await admin_inter.followup.send(
                        f"PR #{pr_number} closed. Request rejected."
                    )

                admin_view = AdminPRView(
                    pr_number=pr_number,
                    nickname=state["nick"],
                    on_confirm=admin_confirm,
                    on_edit=admin_edit,
                    on_reject=admin_reject,
                )

                await staff_channel.send(
                    f"{mention}\n\n"
                    f"{interaction.user.mention} has set their Patreon Minecraft nickname as "
                    f"`{state['nick']}`.\n"
                    f"Please wait for an administrator to approve the change.\n"
                    f"PR: {pr_url}",
                    view=admin_view,
                )

                await interaction.followup.send(
                    "Request submitted. Please wait for an administrator to approve.",
                    ephemeral=True,
                )

            # Ask for Yes/No confirmation in this channel
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

        # If we already know a nickname (e.g. extracted by AI), start directly
        if initial_nickname:
            return await run_flow_with_nick(initial_nickname.strip())

        # Otherwise ask user for nickname first (AI-triggered path)
        await channel.send(
            f"{member.mention} Please reply with your Minecraft nickname "
            f"so we can start the Patreon beta whitelist process."
        )
        return "asked_for_nickname"


def setup(bot: discord.Bot):
    bot.add_cog(AdminCog(bot))
