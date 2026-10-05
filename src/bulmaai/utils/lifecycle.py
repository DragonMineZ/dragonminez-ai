"""Startup/shutdown hooks that survive a hot reload.

on_ready fires once per connection, never after reload_extension, so cogs that started loops, routes or
persistent views there came back half-dead after a reload. A ReloadableCog does that work in on_startup,
which runs once the bot is ready at boot (BulmaAI.on_ready) and right after the cog is (re)loaded later.
on_shutdown is awaited by the update engine before unloading; export_state/import_state carry in-memory
state (raid mode, download tokens...) across the reload through bot.reload_state.
"""

import asyncio
import logging
from typing import Any

from discord.ext import commands


log = logging.getLogger(__name__)


class ReloadableCog(commands.Cog):
    async def on_startup(self) -> None:
        """Start loops, register routes and persistent views, load DB-backed state."""

    async def on_shutdown(self) -> None:
        """Undo on_startup: cancel loops and tasks, unregister routes, close clients."""

    def is_busy(self) -> bool:
        """True while work is in flight that a reload would cut off (e.g. an AI answer being written)."""
        return False

    def export_state(self) -> Any:
        return None

    def import_state(self, state: Any) -> None:
        pass

    async def start_lifecycle(self) -> None:
        if self.__dict__.get("_lifecycle_started"):
            return
        self._lifecycle_started = True
        state = getattr(self.bot, "reload_state", {}).pop(self.qualified_name, None)
        if state is not None:
            try:
                self.import_state(state)
            except Exception:
                log.exception("%s couldn't take back its state after a reload", self.qualified_name)
        await self.on_startup()

    async def stop_lifecycle(self) -> None:
        if not self.__dict__.get("_lifecycle_started"):
            return
        self._lifecycle_started = False
        try:
            state = self.export_state()
        except Exception:
            log.exception("%s couldn't export its state", self.qualified_name)
            state = None
        if state is not None and isinstance(getattr(self.bot, "reload_state", None), dict):
            self.bot.reload_state[self.qualified_name] = state
        await self.on_shutdown()

    def _inject(self, bot):
        cog = super()._inject(bot)
        # Loaded after boot (panel reload, hot update): nothing else will call on_startup for us.
        # The update engine awaits _startup_task to know whether the new code came up.
        if bot.is_ready() is True:
            self._startup_task = asyncio.get_event_loop().create_task(self.start_lifecycle())
            self._startup_task.add_done_callback(self._log_startup_failure)
        return cog

    def _log_startup_failure(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("%s failed to start", self.qualified_name, exc_info=task.exception())

    def cog_unload(self) -> None:
        # Unloaded without the engine awaiting stop_lifecycle first (plain unload_extension): best effort.
        if self.__dict__.get("_lifecycle_started"):
            asyncio.get_event_loop().create_task(self.stop_lifecycle())


def lifecycle_cogs(bot) -> list[ReloadableCog]:
    # Duck-typed so cogs built against an older copy of this module still count.
    return [cog for cog in bot.cogs.values() if hasattr(cog, "start_lifecycle") and hasattr(cog, "stop_lifecycle")]
