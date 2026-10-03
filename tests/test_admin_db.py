import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.admin_db import AdminDatabaseCog, _db_autocomplete
from bulmaai.config import Settings
from bulmaai.utils.permissions import BRUNO_ID

MAIN_GUILD_ID = Settings.panel_guild_id


class DatabaseCommandTests(unittest.IsolatedAsyncioTestCase):
    def ctx(self, user_id, guild_id):
        admin = SimpleNamespace(
            id=user_id, guild=SimpleNamespace(id=guild_id), guild_permissions=SimpleNamespace(administrator=True)
        )
        return SimpleNamespace(author=admin, guild_id=guild_id, respond=AsyncMock(), defer=AsyncMock())

    async def test_only_bruno_in_the_main_server_can_query(self):
        cog = AdminDatabaseCog(SimpleNamespace(settings=SimpleNamespace(panel_guild_id=MAIN_GUILD_ID)))
        pool = AsyncMock()
        with patch("bulmaai.cogs.admin_db.get_pool", pool), patch("discord.Member", SimpleNamespace):
            for ctx in (self.ctx(42, MAIN_GUILD_ID), self.ctx(BRUNO_ID, 2)):
                await cog.database.callback(cog, ctx, "DROP TABLE mod_cases")
                ctx.defer.assert_not_awaited()
        pool.assert_not_awaited()

    async def test_autocomplete_is_empty_for_everyone_else(self):
        ctx = SimpleNamespace(value="", interaction=SimpleNamespace(user=SimpleNamespace(id=42)))
        with patch("bulmaai.cogs.admin_db.get_pool", AsyncMock()) as pool:
            self.assertEqual(await _db_autocomplete(ctx), [])
        pool.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
