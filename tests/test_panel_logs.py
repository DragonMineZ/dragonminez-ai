import asyncio
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import discord_log_forwarding as forwarding
from bulmaai.services import panel_logs


def _record(level: int, **extra) -> logging.LogRecord:
    record = logging.LogRecord("bulmaai.cogs.moderation", level, __file__, 1, "something happened", (), None)
    record.__dict__.update(extra)
    return record


class PanelLogForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def _forward(self, record: logging.LogRecord) -> tuple[AsyncMock, MagicMock]:
        channel = SimpleNamespace(send=AsyncMock())
        bot = MagicMock()
        bot.wait_until_ready = AsyncMock()
        bot.get_channel.return_value = channel
        forwarder = forwarding.DiscordLogForwarder(bot=bot, channel_id=1)
        forwarder._queue.put_nowait(forwarding.build_log_embed_payload(record))
        with patch.object(panel_logs, "record", AsyncMock()) as recorded:
            task = asyncio.create_task(forwarder._send_loop())
            await forwarder._queue.join()
            task.cancel()
        return recorded, channel.send

    async def test_warning_goes_to_panel_only(self):
        recorded, send = await self._forward(_record(logging.WARNING, user_id=123456789012345678))
        recorded.assert_awaited_once()
        self.assertEqual(recorded.await_args.kwargs["user_id"], 123456789012345678)
        self.assertEqual(recorded.await_args.args[0], "moderation")
        send.assert_not_awaited()

    async def test_error_goes_to_panel_and_discord(self):
        recorded, send = await self._forward(_record(logging.ERROR))
        recorded.assert_awaited_once()
        send.assert_awaited_once()


class PanelLogQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_become_numbered_params(self):
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        with patch.object(panel_logs, "get_pool", AsyncMock(return_value=pool)):
            await panel_logs.list_logs(source="case", min_level=30, user_id=5, text="ban", before_id=9, limit=10)
        sql, *args = pool.fetch.await_args.args
        self.assertIn("source = $1", sql)
        self.assertIn("level >= $2", sql)
        self.assertIn("user_id = $3", sql)
        self.assertIn("title ILIKE $4 OR body ILIKE $4", sql)
        self.assertIn("id < $5", sql)
        self.assertEqual(args, ["case", 30, 5, "%ban%", 9, 10])



if __name__ == "__main__":
    unittest.main()
