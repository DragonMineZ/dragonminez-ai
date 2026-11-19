import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bulmaai.services import discord_log_forwarding, log_forum

POOL = "bulmaai.services.log_forum.get_pool"


def thread(thread_id, *, parent_id=10, name="⚠️ Warns", archived=False, locked=False):
    item = MagicMock(spec=discord.Thread)
    item.id, item.parent_id, item.name, item.archived, item.locked = thread_id, parent_id, name, archived, locked
    item.edit = AsyncMock(return_value=item)
    return item


def forum(*, threads=(), tags=(), known=None):
    item = MagicMock(spec=discord.ForumChannel)
    item.id = 10
    item.threads = list(threads)
    item.available_tags = list(tags)
    item.guild = SimpleNamespace(get_thread=lambda thread_id: (known or {}).get(thread_id))
    item.create_thread = AsyncMock(return_value=thread(99))
    return item


def bot(channel, fetched=None):
    return SimpleNamespace(
        get_channel=lambda channel_id: channel if channel_id == 10 else None,
        fetch_channel=AsyncMock(side_effect=fetched or discord.NotFound(MagicMock(status=404), "gone")),
    )


def pool(stored=None):
    return MagicMock(fetchval=AsyncMock(return_value=stored), execute=AsyncMock())


class LogForumTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        log_forum._threads.clear()
        log_forum._locks.clear()

    async def test_text_channel_gets_everything(self):
        channel = SimpleNamespace(send=AsyncMock())
        self.assertIs(await log_forum.destination(bot(channel), 10, "warns"), channel)

    async def test_no_channel_or_unreachable_is_none(self):
        self.assertIsNone(await log_forum.destination(bot(None), None, "warns"))
        self.assertIsNone(await log_forum.destination(bot(None), 10, "warns"))

    async def test_forum_makes_the_post_once_and_remembers_it(self):
        target = forum(tags=[SimpleNamespace(name="warns"), SimpleNamespace(name="Bans")])
        db = pool()
        with patch(POOL, AsyncMock(return_value=db)):
            first = await log_forum.destination(bot(target), 10, "warns")
            target.guild = SimpleNamespace(get_thread=lambda thread_id: first)
            again = await log_forum.destination(bot(target), 10, "warns")
        self.assertIs(first, again)
        target.create_thread.assert_awaited_once()
        kwargs = target.create_thread.await_args.kwargs
        self.assertEqual(kwargs["name"], "⚠️ Warns")
        self.assertEqual([tag.name for tag in kwargs["applied_tags"]], ["warns"])
        self.assertEqual(db.execute.await_args.args[1:], (10, "warns", 99))

    async def test_stored_post_is_reused_and_reopened(self):
        quiet = thread(50, archived=True)
        target = forum(known={50: quiet})
        with patch(POOL, AsyncMock(return_value=pool(stored=50))):
            self.assertIs(await log_forum.destination(bot(target), 10, "warns"), quiet)
        quiet.edit.assert_awaited_once_with(archived=False)
        target.create_thread.assert_not_awaited()

    async def test_deleted_post_is_made_again(self):
        target = forum()
        db = pool(stored=50)
        with patch(POOL, AsyncMock(return_value=db)):
            made = await log_forum.destination(bot(target), 10, "bans")
        self.assertEqual(made.id, 99)
        self.assertEqual(target.create_thread.await_args.kwargs["name"], "🔨 Bans")
        self.assertEqual(db.execute.await_args.args[1:], (10, "bans", 99))

    async def test_post_staff_made_by_name_is_adopted(self):
        mine = thread(70, name="🤖 Automod")
        target = forum(threads=[thread(71, name="🔨 Bans"), mine])
        db = pool()
        with patch(POOL, AsyncMock(return_value=db)):
            self.assertIs(await log_forum.destination(bot(target), 10, "automod"), mine)
        target.create_thread.assert_not_awaited()
        self.assertEqual(db.execute.await_args.args[1:], (10, "automod", 70))

    def test_actions_map_to_posts(self):
        self.assertEqual(
            [log_forum.post_for_action(action) for action in ("warn", "verbal", "untimeout", "kick", "softban", "note")],
            ["warns", "warns", "timeouts", "kicks", "bans", "commands"],
        )
        self.assertTrue(set(log_forum.ACTION_POSTS.values()) <= set(log_forum.MOD_LOG_POSTS))

    def test_own_warnings_are_never_forwarded(self):
        # The forwarder posts through log_forum; forwarding its failures would loop.
        self.assertFalse(discord_log_forwarding.is_moderation_source(log_forum.__name__.split(".")[-1]))


if __name__ == "__main__":
    unittest.main()
