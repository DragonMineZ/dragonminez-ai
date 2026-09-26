"""Polls origin for new commits and pings Bruno with a "Pull & restart" button.

The button pulls (fast-forward only), installs requirements if they changed, checks that every
extension still imports, then exits non-zero so systemd (Restart=always or on-failure) brings the
bot back on the new code. A failed check rolls the checkout back and keeps the old process running.
"""

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands, tasks

from bulmaai.bot import REPO_ROOT
from bulmaai.utils.permissions import BRUNO_ID


log = logging.getLogger(__name__)

POLL_MINUTES = 3
APPLY_BUTTON_ID = "self_update:apply"
IMPORT_CHECK = (
    "import importlib, bulmaai.bot\n"
    "from bulmaai.config import load_settings\n"
    "for ext in load_settings().initial_extensions: importlib.import_module(ext)\n"
)


async def run(*args: str, timeout: float = 300) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=REPO_ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"{args[0]} timed out after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace").strip()


async def git(*args: str) -> str:
    code, out = await run("git", *args)
    if code != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{out}")
    return out


def _tail(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


async def apply_update() -> tuple[bool, str]:
    """Pull, install, import-check. Returns (ok, message); on failure the checkout is rolled back."""
    old = await git("rev-parse", "HEAD")
    code, out = await run("git", "pull", "--ff-only")
    if code != 0:
        return False, f"`git pull --ff-only` failed, nothing changed:\n```\n{_tail(out)}\n```"
    new = await git("rev-parse", "HEAD")
    if new == old:
        return False, "Already up to date, nothing to restart for."

    steps = []
    if "requirements.txt" in (await git("diff", "--name-only", old, new)).splitlines():
        steps.append(("pip install", (sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt")))
    steps.append(("import check", (sys.executable, "-c", IMPORT_CHECK)))
    for name, cmd in steps:
        code, out = await run(*cmd)
        if code != 0:
            # --keep refuses instead of clobbering local edits on the VPS.
            await run("git", "reset", "--keep", old)
            return False, f"{name} failed, rolled back to `{old[:7]}`:\n```\n{_tail(out)}\n```"
    return True, f"Updated `{old[:7]}` → `{new[:7]}`. Restarting…"


class ApplyUpdateView(discord.ui.View):
    def __init__(self, cog: "SelfUpdateCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Pull & restart", style=discord.ButtonStyle.danger, custom_id=APPLY_BUTTON_ID)
    async def apply(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.user.id != BRUNO_ID:
            await interaction.response.send_message("Only Bruno can restart the bot.", ephemeral=True)
            return
        if self.cog.lock.locked():
            await interaction.response.send_message("An update is already running.", ephemeral=True)
            return
        await interaction.response.defer()
        async with self.cog.lock:
            try:
                ok, message = await apply_update()
            except Exception as error:
                log.exception("Self-update failed")
                ok, message = False, f"Update crashed: `{error}`"
            log.warning("Self-update by %s: %s", interaction.user, message.splitlines()[0])
            await interaction.followup.send(message)
            if not ok:
                return
            button.disabled = True
            button.label = "Restarting…"
            await interaction.message.edit(view=self)
            self.cog.bot.restart_requested = True
            await self.cog.bot.close()


class SelfUpdateCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.notified_sha: str | None = None
        self._ready = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._ready:
            return
        self._ready = True
        self.bot.add_view(ApplyUpdateView(self))
        self.poll.start()

    def cog_unload(self) -> None:
        self.poll.cancel()

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll(self) -> None:
        if self.lock.locked():
            return
        try:
            await self._poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Self-update poll failed")

    async def _poll_once(self) -> None:
        await git("fetch", "--quiet")
        upstream = await git("rev-parse", "@{u}")
        commits = await git("log", "--oneline", "--no-decorate", "HEAD..@{u}")
        if not commits or upstream == self.notified_sha:
            return
        channel_id = self.bot.settings.discord_log_channel_id
        channel = channel_id and (self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id))
        if channel is None:
            log.warning("New commits on origin but DISCORD_LOG_CHANNEL_ID is unset; can't prompt for restart.")
            return
        count = len(commits.splitlines())
        await channel.send(
            f"<@{BRUNO_ID}> {count} new commit{'s' * (count != 1)} on `{await git('rev-parse', '--abbrev-ref', '@{u}')}`:\n"
            f"```\n{_tail(commits)}\n```",
            view=ApplyUpdateView(self),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(BRUNO_ID)], everyone=False, roles=False),
        )
        # ponytail: in-memory, so a restart for other reasons re-pings once; persist if that gets noisy.
        self.notified_sha = upstream


def setup(bot: discord.Bot):
    bot.add_cog(SelfUpdateCog(bot))
