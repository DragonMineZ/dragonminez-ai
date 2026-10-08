"""Polls origin for new commits and gets them into the running bot with as little downtime as possible.

Each new upstream commit is planned (services/update_plan.py) and checked in a staging worktree, running
only the tests that cover what changed. Docs/test-only updates just fast-forward; hot-reloadable ones are
applied in-process on their own (cogs, services, panel routes, prompts) with rollback on failure. Anything
that needs a real restart (requirements, bot.py, config, stateful modules) pings Bruno with the old
"Pull & restart" card: pull, pip install if needed, import check, full test suite, then exit non-zero so
systemd brings the bot back on the new code.
"""

import asyncio
import logging
import sys

import discord
from discord.ext import commands, tasks

from bulmaai.services import panel_logs, update_engine
from bulmaai.services.update_engine import git, run, tail
from bulmaai.services.update_plan import plan_update
from bulmaai.utils.lifecycle import ReloadableCog
from bulmaai.utils.permissions import BRUNO_ID
from bulmaai.ui.v2 import card, trim


log = logging.getLogger(__name__)

POLL_MINUTES = 3
APPLY_BUTTON_ID = "self_update:apply"
IMPORT_CHECK = (
    "import importlib, bulmaai.bot\n"
    "from bulmaai.config import load_settings\n"
    "for ext in load_settings().initial_extensions: importlib.import_module(ext)\n"
)
HOT_COLOR = discord.Colour.from_rgb(46, 204, 113)
FAILED_COLOR = discord.Colour.from_rgb(231, 76, 60)
RESTART_COLOR = discord.Colour.from_rgb(241, 196, 15)
BRUNO_ONLY = discord.AllowedMentions(users=[discord.Object(BRUNO_ID)], everyone=False, roles=False)


async def apply_update() -> tuple[bool, str]:
    """Full-restart path. Pull, install, import-check, test. On failure the checkout is rolled back."""
    old = await git("rev-parse", "HEAD")
    code, out = await run("git", "pull", "--ff-only")
    if code != 0:
        return False, f"`git pull --ff-only` failed, nothing changed:\n```\n{tail(out)}\n```"
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
            return False, f"{name} failed, rolled back to `{old[:7]}`:\n```\n{tail(out)}\n```"
    ok, report = await update_engine.run_full_suite(update_engine.REPO_ROOT)
    if not ok:
        await run("git", "reset", "--keep", old)
        return False, f"Full test suite failed, rolled back to `{old[:7]}`:\n{report}"
    return True, f"Updated `{old[:7]}` → `{new[:7]}`. Restarting…"


def apply_button(label: str = "Pull & restart", *, disabled: bool = False) -> discord.ui.Button:
    return discord.ui.Button(label=label, style=discord.ButtonStyle.danger, custom_id=APPLY_BUTTON_ID, disabled=disabled)


def _with_apply_button(message: discord.Message, label: str, *, disabled: bool) -> discord.ui.DesignerView:
    """The restart prompt card as it is, with the Pull & restart button relabelled."""
    view = discord.ui.DesignerView.from_message(message, timeout=None)
    for container in view.children:
        for item in getattr(container, "items", []):
            if isinstance(item, discord.ui.ActionRow):
                container.remove_item(item)
                container.add_item(discord.ui.ActionRow(apply_button(label, disabled=disabled)))
                return view
    return view


class SelfUpdateCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.handled_sha: str | None = None

    async def on_startup(self) -> None:
        if not self.poll.is_running():
            self.poll.start()

    async def on_shutdown(self) -> None:
        self.poll.cancel()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type == discord.InteractionType.component and (interaction.data or {}).get("custom_id") == APPLY_BUTTON_ID:
            await self._apply(interaction)

    async def _apply(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.user.id != BRUNO_ID:
            await interaction.response.send_message("Only Bruno can restart the bot.", ephemeral=True)
            return
        bot = self.bot
        if bot.update_lock.locked():
            await interaction.response.send_message("An update is already running.", ephemeral=True)
            return
        # Pull + full suite takes a while, so show it's working right away instead of a silent defer.
        message = interaction.message
        await interaction.response.edit_message(view=_with_apply_button(message, "Pulling & testing…", disabled=True))
        async with bot.update_lock:
            old = await git("rev-parse", "HEAD")
            try:
                ok, result = await apply_update()
            except Exception as error:
                log.exception("Self-update failed")
                ok, result = False, f"Update crashed: `{error}`"
            log.warning("Self-update by %s: %s", interaction.user, result.splitlines()[0])
            await interaction.followup.send(view=card(tail(result, 3800), color=HOT_COLOR if ok else FAILED_COLOR))
            new = await git("rev-parse", "HEAD")
            await update_engine.record_update(
                sha_from=old, sha_to=new, mode="full", result="restarting" if ok else "failed", detail=result
            )
            if not ok:
                await message.edit(view=_with_apply_button(message, "Pull & restart", disabled=False))
                return
            await message.edit(view=_with_apply_button(message, "Restarting…", disabled=True))
            bot.restart_requested = True
            await bot.close()

    def export_state(self) -> str | None:
        return self.handled_sha

    def import_state(self, state: str) -> None:
        self.handled_sha = state

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll(self) -> None:
        if self.bot.update_lock.locked():
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
        head = await git("rev-parse", "HEAD")
        if upstream in (head, self.handled_sha):
            return
        if not await git("log", "--oneline", "HEAD..@{u}"):
            return  # local checkout is ahead, nothing to take
        # ponytail: in-memory (carried across hot reloads), so a full restart re-handles the same sha once.
        self.handled_sha = upstream
        # Detached from the poll loop: a hot update may reload this very cog, which cancels the loop.
        self.bot.update_task = asyncio.get_running_loop().create_task(self.handle_update(head, upstream))

    async def handle_update(self, head: str, upstream: str) -> None:
        async with self.bot.update_lock:
            try:
                await self._handle_update(head, upstream)
            except Exception as error:
                log.exception("Self-update of %s failed", upstream[:7])
                await self._send(f"❌ Update to `{upstream[:7]}` crashed: `{error}`", color=FAILED_COLOR, ping=True)

    async def _handle_update(self, head: str, upstream: str) -> None:
        commits = await git("log", "--oneline", "--no-decorate", f"{head}..{upstream}")
        branch = await git("rev-parse", "--abbrev-ref", "@{u}")
        changed = await update_engine.changed_paths(head, upstream)
        staging = await update_engine.prepare_staging(upstream)
        plan = plan_update(
            changed,
            src_root=staging / "src",
            tests_root=staging / "tests",
            loaded_extensions=self.bot.extensions,
        )

        reasons = list(plan.reasons)
        if plan.mode != "full":
            if not self.bot.settings.self_update_auto_apply:
                reasons.append("auto-apply is turned off in the panel settings")
            elif await update_engine.is_dirty():
                reasons.append("the live checkout has local edits to tracked files")
        if plan.mode == "full" or reasons:
            await self._prompt_restart(commits, branch, reasons)
            await update_engine.record_update(
                sha_from=head, sha_to=upstream, mode="full", result="prompted", detail="\n".join(reasons)
            )
            return

        count = len(commits.splitlines())
        if plan.mode == "noop":
            await git("merge", "--ff-only", "--quiet", upstream)
            await self._send(f"⏩ Fast-forwarded {count} commit{'s' * (count != 1)} to `{upstream[:7]}`, nothing to reload.", color=HOT_COLOR)
            await update_engine.record_update(sha_from=head, sha_to=upstream, mode="noop", result="applied", duration_ms=0)
            return

        ok, report = await update_engine.run_checks(staging, plan)
        if not ok:
            await self._prompt_restart(commits, branch, [f"checks failed on the new code, so it wasn't hot-applied: {report}"])
            await update_engine.record_update(
                sha_from=head, sha_to=upstream, mode="hot", result="checks_failed", detail=report
            )
            return

        result = await update_engine.apply_hot(self.bot, plan, head, upstream)
        await update_engine.record_update(
            sha_from=head,
            sha_to=upstream,
            mode="hot",
            result="applied" if result.ok else ("restarting" if result.restart_needed else "rolled_back"),
            duration_ms=result.duration_ms,
            detail="\n".join(result.errors) or plan.summary(),
        )
        await self._send(*self._hot_report(plan, result, commits, report, head, upstream), ping=not result.ok)
        if result.restart_needed:
            self.bot.restart_requested = True
            await self.bot.close()

    def _hot_report(self, plan, result, commits: str, report: str, head: str, upstream: str) -> tuple[str, str, int]:
        """(title, body, colour) of a hot update's DM card."""
        if result.ok:
            title = f"🔥 Hot update applied `{head[:7]}` → `{upstream[:7]}`"
        elif result.restart_needed:
            title = f"❌ Hot update to `{upstream[:7]}` failed and so did the rollback, restarting"
        else:
            title = f"❌ Hot update to `{upstream[:7]}` failed, rolled back to `{head[:7]}`"
        parts = [
            f"**Commits**\n```\n{tail(commits, 1000)}\n```",
            f"**Reloaded**\n{tail(plan.summary(), 800)}",
            f"**Checks**\n{tail(report, 800)}",
            f"**Took** {result.duration_ms} ms　**Commands synced** {'yes' if result.synced_commands else 'no change'}",
        ]
        if result.errors:
            parts.append(f"**Error**\n```\n{tail(chr(10).join(result.errors), 800)}\n```")
        return title, "\n".join(parts), HOT_COLOR if result.ok else FAILED_COLOR

    async def _prompt_restart(self, commits: str, branch: str, reasons: list[str]) -> None:
        count = len(commits.splitlines())
        why = "\n".join(f"- {reason}" for reason in reasons)
        await self._send(
            f"🔁 {count} new commit{'s' * (count != 1)} on `{branch}` need a full restart",
            f"{tail(why, 600)}\n```\n{tail(commits, 900)}\n```",
            RESTART_COLOR,
            ping=True,
            buttons=[apply_button()],
        )

    async def _send(
        self, title: str, body: str = "", color: int | None = None, *, ping: bool = False, buttons=None
    ) -> None:
        """Update news, prompts and failures all go to Bruno's DMs as a card; the panel keeps a copy of the text.
        ping: the card mentions Bruno (a DM still notifies without it; this marks the ones that need him)."""
        await panel_logs.record("update", title[:200], body)
        text = (f"<@{BRUNO_ID}> " if ping else "") + f"### {title}" + (f"\n{body}" if body else "")
        try:
            owner = self.bot.get_user(BRUNO_ID) or await self.bot.fetch_user(BRUNO_ID)
            view = card(trim(text, 3800), color=color, buttons=buttons)
            await owner.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Couldn't DM Bruno the self-update news: %s", title, exc_info=True)


def setup(bot: discord.Bot):
    bot.add_cog(SelfUpdateCog(bot))
