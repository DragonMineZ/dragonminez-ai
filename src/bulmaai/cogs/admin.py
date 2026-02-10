import logging

import discord
from discord import slash_command

from src.bulmaai.github.github_app_auth import GitHubAppAuth
from src.bulmaai.github.github_whitelist import GitHubWhitelistService
from src.bulmaai.ui.patreon_views import UserConfirmView, AdminPRView, MC_NAME_RE
from src.bulmaai.utils.permissions import is_admin, has_any_allowed_role

log = logging.getLogger(__name__)

ALLOWED_ROLE_ID_1 = 1287877272224665640
ALLOWED_ROLE_ID_2 = 1287877305259130900
ADMIN_PING_ROLE_ID = 1309022450671161476
STAFF_CHANNEL_ID = 1470178423862460510  # Ticket 0201


def _pick_staff_channel(ctx: discord.ApplicationContext) -> discord.abc.Messageable:
    if STAFF_CHANNEL_ID and ctx.guild:
        ch = ctx.guild.get_channel(STAFF_CHANNEL_ID)
        if ch is not None:
            return ch
    return ctx.channel


class AdminCog(discord.Cog):
    """Admin commands for the bot (temporary home for /github)."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.gh = self._build_github_service()

    def _build_github_service(self) -> GitHubWhitelistService:
        # All secrets stay in .env
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
        import os
        v = os.getenv(name)
        if not v:
            raise RuntimeError(f"Missing env var: {name}")
        return v

    @staticmethod
    def _env_default(name: str, default: str) -> str:
        import os
        return os.getenv(name, default)

    @slash_command(
        name="github",
        description="Patreon: set your MC nickname for beta access (creates a GitHub PR).",
        dm_permission=False,  # guild-only
    )
    async def github(self, ctx: discord.ApplicationContext, github_username: str):
        await ctx.defer(ephemeral=True)

        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return await ctx.respond("This command can only be used in a server.", ephemeral=True)

        member: discord.Member = ctx.author

        # Role-gated: 2 roles allowed; administrators bypass
        if not is_admin(member) and not has_any_allowed_role(member, (ALLOWED_ROLE_ID_1, ALLOWED_ROLE_ID_2)):
            return await ctx.respond("You don’t have permission to use this command.", ephemeral=True)

        nickname = github_username.strip()
        if not MC_NAME_RE.match(nickname):
            return await ctx.respond("Nickname must be 3–16 chars, letters/numbers/_ only.", ephemeral=True)

        async def on_user_confirm(interaction: discord.Interaction, initial_nick: str):
            # Use a mutable state so "Edit Nickname" can update what "Confirm" later reports.
            state = {"nick": initial_nick}

            safe_user = interaction.user.name.replace(" ", "_")
            branch = f"patreon/{safe_user}-{interaction.user.id}"

            # 1) Create/update PR branch with the new nickname appended
            await self.gh.create_branch(branch, self.gh.base_branch)

            base_text, base_sha = await self.gh.get_file(self.gh.file_path, ref=self.gh.base_branch)
            base_lines = [ln.strip() for ln in base_text.splitlines() if ln.strip()]

            if state["nick"] in base_lines:
                return await interaction.followup.send(
                    f"`{state['nick']}` is already whitelisted. Nothing to do.",
                    ephemeral=True,
                )

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
            admin_role = ctx.guild.get_role(ADMIN_PING_ROLE_ID)
            mention = admin_role.mention if admin_role else f"<@&{ADMIN_PING_ROLE_ID}>"
            staff_channel = _pick_staff_channel(ctx)

            admin_view: AdminPRView | None = None

            async def admin_confirm(admin_inter: discord.Interaction):
                await self.gh.merge_pr(pr_number)
                await self.gh.add_comment(pr_number, f"Request approved by {admin_inter.user}, PR merged.")
                await self.gh.remove_branch(pr_number)
                await admin_inter.followup.send(f"PR #{pr_number} merged. `{state['nick']}` approved.")

            async def admin_edit(admin_inter: discord.Interaction, new_nick: str):
                old_nick = state["nick"]

                # Update the PR branch file: remove old nick, add new nick
                branch_text, branch_sha = await self.gh.get_file(self.gh.file_path, ref=branch)
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

                await admin_inter.followup.send(f"Updated PR branch nickname to `{new_nick}`.")

            async def admin_reject(admin_inter: discord.Interaction):
                await self.gh.close_pr(pr_number)
                await self.gh.add_comment(pr_number, f"Request rejected by {admin_inter.user}, PR closed.")
                await self.gh.remove_branch(pr_number)
                await admin_inter.followup.send(f"PR #{pr_number} closed. Request rejected.")

            admin_view = AdminPRView(
                pr_number=pr_number,
                nickname=state["nick"],
                on_confirm=admin_confirm,
                on_edit=admin_edit,
                on_reject=admin_reject,
            )

            await staff_channel.send(
                f"{mention}\n\n"
                f"{interaction.user.mention} has set their Patreon Minecraft nickname as `{state['nick']}`.\n"
                f"Please wait for an administrator to approve the change.\n"
                f"PR: {pr_url}",
                view=admin_view,
            )

            await interaction.followup.send(
                "Request submitted. Please wait for an administrator to approve.",
                ephemeral=True,
            )
            return None

        await ctx.respond(
            f"You've said that `{nickname}` is your Minecraft nickname to get access to the Patreon-only releases, is this correct?",
            ephemeral=True,
            view=UserConfirmView(requester_id=ctx.author.id, nickname=nickname, on_confirm=on_user_confirm),
        )
        return None


def setup(bot: discord.Bot):
    bot.add_cog(AdminCog(bot))
