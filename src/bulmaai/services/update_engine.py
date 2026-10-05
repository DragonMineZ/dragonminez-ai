"""Runs an UpdatePlan: checks the new revision in a staging worktree, then hot-swaps it into the live bot.

The live checkout is only touched after the staging checks pass. A hot apply that fails anywhere rolls the
checkout back and runs the same reload over the old code; if even that fails the bot restarts.
"""

import asyncio
import hashlib
import importlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord

from bulmaai.services import update_plan
from bulmaai.services.update_plan import UpdatePlan


log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
STAGING_NAME = ".update-staging"
BUSY_WAIT_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 300
# Same dummies as CI so tests never pick up the live token or keys.
TEST_ENV = {"DISCORD_TOKEN": "dummy", "OPENAI_KEY": "dummy", "GH_APP_PRIVATE_KEY_PEM": "dummy"}


async def run(*args: str, cwd: Path | None = None, env: dict | None = None, timeout: float = CHECK_TIMEOUT_SECONDS) -> tuple[int, str]:
    cwd = cwd or REPO_ROOT
    env = {**os.environ, "PYTHONPATH": str(cwd / "src"), "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"{args[0]} timed out after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace").strip()


async def git(*args: str, cwd: Path | None = None) -> str:
    code, out = await run("git", *args, cwd=cwd)
    if code != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{out}")
    return out


def tail(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


async def is_dirty() -> bool:
    # Untracked files (settings overrides, transcripts) don't block a fast-forward; edited tracked ones might.
    return bool(await git("status", "--porcelain", "--untracked-files=no"))


async def changed_paths(old: str, new: str) -> list[str]:
    return (await git("diff", "--name-only", old, new)).splitlines()


async def prepare_staging(sha: str) -> Path:
    staging = REPO_ROOT / STAGING_NAME
    if (staging / ".git").exists():
        await git("checkout", "--detach", "--force", sha, cwd=staging)
        await git("clean", "-fdq", cwd=staging)
    else:
        await run("git", "worktree", "prune")
        await git("worktree", "add", "--detach", "--force", str(staging), sha)
    return staging


def import_check_script(modules: list[str]) -> str:
    return "import importlib, bulmaai.bot\n" + "".join(f"importlib.import_module({name!r})\n" for name in modules)


async def run_checks(root: Path, plan: UpdatePlan) -> tuple[bool, str]:
    """Imports what will reload and runs only the tests that cover it (all of them for a full restart)."""
    code, out = await run(sys.executable, "-c", import_check_script(plan.check_imports), cwd=root)
    if code != 0:
        return False, f"import check failed:\n```\n{tail(out)}\n```"
    if plan.mode == "full":
        args = ["discover", "-s", "tests"]
    elif plan.tests:
        args = plan.tests
    else:
        return True, "imports ok, no tests cover this change"
    env = {**TEST_ENV, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")])}
    code, out = await run(sys.executable, "-m", "unittest", *args, cwd=root, env=env)
    if code != 0:
        return False, f"tests failed:\n```\n{tail(out)}\n```"
    ran = next((line for line in reversed(out.splitlines()) if line.startswith("Ran ")), "tests passed")
    return True, f"imports ok, {ran.lower()} ({'all' if plan.mode == 'full' else len(plan.tests)} modules)"


@dataclass
class HotResult:
    ok: bool
    message: str
    duration_ms: int = 0
    synced_commands: bool = False
    rolled_back: bool = False
    restart_needed: bool = False
    errors: list[str] = field(default_factory=list)


def _commands_fingerprint(bot: discord.Bot) -> str | None:
    try:
        payload = sorted(
            (json.dumps(command.to_dict(), sort_keys=True, default=str) for command in bot.pending_application_commands)
        )
    except Exception:
        return None
    return hashlib.sha256("\n".join(payload).encode()).hexdigest()


async def _wait_idle(cogs: list) -> None:
    deadline = time.monotonic() + BUSY_WAIT_SECONDS
    while time.monotonic() < deadline and any(cog.is_busy() for cog in cogs if hasattr(cog, "is_busy")):
        await asyncio.sleep(1)


def _cogs_of(bot: discord.Bot, extensions: list[str]) -> list:
    names = set(extensions)
    return [cog for cog in bot.cogs.values() if cog.__module__ in names]


def _clear_caches() -> None:
    for name, module in list(sys.modules.items()):
        if module is None or not (name == update_plan.PACKAGE or name.startswith(update_plan.PACKAGE + ".")):
            continue
        for value in list(vars(module).values()):
            if callable(getattr(value, "cache_clear", None)) and getattr(value, "__module__", None) == name:
                value.cache_clear()


async def reload_in_process(bot: discord.Bot, plan: UpdatePlan, extensions: list[str]) -> None:
    """Swap the planned modules and cogs for whatever is on disk now. Raises on the first failure.

    extensions is fixed by the caller before the first attempt, so a rollback also brings back
    extensions that the failed attempt left unloaded.
    """
    old_cogs = _cogs_of(bot, extensions)
    await _wait_idle(old_cogs)

    for cog in old_cogs:
        if hasattr(cog, "stop_lifecycle"):
            await cog.stop_lifecycle()
    panel_was_up = plan.rebind_panel and bot.panel_server is not None
    if plan.rebind_panel:
        await bot.stop_panel()
    for name in extensions:
        if name in bot.extensions:
            bot.unload_extension(name)

    for name in plan.modules_to_reload:
        module = sys.modules.get(name)
        if module is not None:
            importlib.reload(module)

    for name in extensions:
        bot.load_extension(name)
    for cog in _cogs_of(bot, extensions):
        task = getattr(cog, "_startup_task", None)
        if task is not None:
            await task  # re-raises if on_startup failed
        elif hasattr(cog, "start_lifecycle"):
            await cog.start_lifecycle()

    if plan.rebind_panel:
        await bot.start_panel()
        if panel_was_up and bot.panel_server is None:
            raise RuntimeError("The panel didn't come back up")
    if plan.clear_caches:
        _clear_caches()
    if plan.rerun_schema:
        from bulmaai.services import db_schema

        await db_schema.ensure_schema()


async def apply_hot(bot: discord.Bot, plan: UpdatePlan, old_sha: str, new_sha: str) -> HotResult:
    started = time.monotonic()
    before = _commands_fingerprint(bot)
    extensions = [name for name in plan.extensions_to_reload if name in bot.extensions]
    await git("merge", "--ff-only", "--quiet", new_sha)
    result = HotResult(ok=True, message="")
    try:
        await reload_in_process(bot, plan, extensions)
    except Exception as error:
        log.exception("Hot update to %s failed; rolling back", new_sha[:7])
        result = HotResult(ok=False, message=f"Hot update failed: `{error}`", rolled_back=True, errors=[str(error)])
        try:
            await git("reset", "--keep", old_sha)
            await reload_in_process(bot, plan, extensions)
        except Exception as rollback_error:
            log.exception("Rolling back the hot update failed too; restarting")
            result.errors.append(str(rollback_error))
            result.restart_needed = True

    after = _commands_fingerprint(bot)
    if before is None or after is None or before != after:
        try:
            await bot.sync_commands()
            result.synced_commands = True
        except Exception:
            log.exception("Slash command sync after hot update failed")
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


async def run_full_suite(root: Path) -> tuple[bool, str]:
    if not (root / "tests").is_dir():
        return True, "no tests"
    code, out = await run(
        sys.executable, "-m", "unittest", "discover", "-s", "tests", cwd=root,
        env={**TEST_ENV, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")])},
    )
    if code != 0:
        return False, f"tests failed:\n```\n{tail(out)}\n```"
    return True, "tests passed"


async def reload_extension_safely(bot: discord.Bot, name: str) -> bool:
    """Manual reload of one cog (panel button). py-cord puts the old code back if the new one won't load.

    Returns whether slash commands were re-synced.
    """
    async with bot.update_lock:
        before = _commands_fingerprint(bot)
        for cog in _cogs_of(bot, [name]):
            if hasattr(cog, "stop_lifecycle"):
                await cog.stop_lifecycle()
        try:
            bot.reload_extension(name)
        finally:
            # Either the new cog or the restored old one: both were injected while ready and start themselves.
            for cog in _cogs_of(bot, [name]):
                task = getattr(cog, "_startup_task", None)
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
        if before is not None and before == _commands_fingerprint(bot):
            return False
        await bot.sync_commands()
        return True


async def record_update(
    *, sha_from: str, sha_to: str, mode: str, result: str, duration_ms: int | None = None, detail: str | None = None
) -> None:
    try:
        from bulmaai.database.db import get_pool

        pool = await get_pool()
        await pool.execute(
            "INSERT INTO bot_updates (sha_from, sha_to, mode, result, duration_ms, detail) VALUES ($1, $2, $3, $4, $5, $6)",
            sha_from,
            sha_to,
            mode,
            result,
            duration_ms,
            (detail or "")[:2000] or None,
        )
    except Exception:
        log.warning("Couldn't record the %s update %s -> %s", mode, sha_from[:7], sha_to[:7], exc_info=True)


async def recent_updates(limit: int = 10) -> list[dict]:
    from bulmaai.database.db import get_pool

    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT sha_from, sha_to, mode, result, duration_ms, detail, applied_at FROM bot_updates "
        "ORDER BY applied_at DESC LIMIT $1",
        limit,
    )
    return [dict(row) for row in rows]
