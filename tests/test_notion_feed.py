import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.notion_feed import NotionFeedCog
from bulmaai.services import notion, notion_feed
from bulmaai.ui import notion_cards
from tests.v2_helpers import texts

DS = "41cb2ae3-2ac2-811e-aab2-000bac58bf46"
PAGE_ID = "11111111-2222-3333-4444-555555555555"
FORUM_ID = 900


def page(*, status="Inbox", priority="P2 Normal", title="Ki blast crash", project="Mod", parent=DS, trash=False,
         people=("Goku",)) -> dict:
    return {
        "id": PAGE_ID,
        "url": "https://www.notion.so/x",
        "in_trash": trash,
        "parent": {"type": "data_source_id", "data_source_id": parent.replace("-", "")},
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": title}]},
            "ID": {"type": "unique_id", "unique_id": {"prefix": "DMZ", "number": 12}},
            "Type": {"type": "select", "select": {"name": "Bug"}},
            "Status": {"type": "status", "status": {"name": status}},
            "Project": {"type": "select", "select": {"name": project}},
            "Priority": {"type": "select", "select": {"name": priority}},
            "Assignee": {"type": "people", "people": [{"name": name} for name in people]},
        },
    }


def event(kind="page.properties_updated", *, event_id="ev-1", entity=PAGE_ID, author_type="person", data=None) -> dict:
    return {
        "id": event_id, "type": kind, "entity": {"id": entity, "type": "comment" if kind.startswith("comment") else "page"},
        "authors": [{"id": "user-1", "type": author_type}], "data": data or {},
    }


def sign(token: str, body: bytes) -> str:
    return "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()


class FakeThread:
    def __init__(self, thread_id: int, *, name: str, applied_tags=(), archived=False, locked=False):
        self.id = thread_id
        self.name = name
        self.applied_tags = list(applied_tags)
        self.archived = archived
        self.locked = locked
        self.sent: list[str] = []
        self.edits: list[dict] = []
        self.starter = SimpleNamespace(edit=AsyncMock())

    async def edit(self, **changes):
        self.edits.append(changes)
        for key, value in changes.items():
            setattr(self, key, value)
        return self

    async def send(self, *, view, allowed_mentions):
        assert allowed_mentions.everyone is False and allowed_mentions.users is False
        self.sent.append("\n".join(texts(view)))
        self.archived = False  # Discord opens an archived post you post into

    def get_partial_message(self, message_id):
        assert message_id == self.id
        return self.starter


class Harness:
    def __init__(self):
        self.rows: dict[str, notion_feed.Post] = {}
        self.created = 0
        self.threads: dict[int, FakeThread] = {}
        self.forum = MagicMock(spec=discord.ForumChannel)
        self.forum.id = FORUM_ID
        self.forum.available_tags = []
        self.forum.guild = SimpleNamespace(get_thread=lambda thread_id: self.threads.get(thread_id))
        self.forum.edit = AsyncMock(side_effect=self._edit_forum)
        self.forum.create_thread = AsyncMock(side_effect=self._create_thread)
        bot = SimpleNamespace(
            get_channel=lambda channel_id: self.forum if channel_id == FORUM_ID else None,
            fetch_channel=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")),
        )
        self.cog = NotionFeedCog(bot)
        self.cog.settings = SimpleNamespace(
            notion_token="secret", notion_tracker_data_source_id=DS, notion_forum_channel_id=FORUM_ID,
            notion_webhook_token="whsec",
        )
        self.page = page()
        self.comment = {"text": "Looks fixed on main", "author_id": "user-2", "author_type": "person", "page_id": PAGE_ID}

    async def _edit_forum(self, *, available_tags):
        for index, tag in enumerate(available_tags, start=1):
            tag.id = tag.id or index
        self.forum.available_tags = list(available_tags)
        return None

    async def _create_thread(self, *, name, view, applied_tags, allowed_mentions):
        assert allowed_mentions.everyone is False
        self.created += 1
        thread = FakeThread(1000 + self.created, name=name, applied_tags=applied_tags)
        thread.card = texts(view)
        self.threads[thread.id] = thread
        return thread

    async def _get_post(self, page_id):
        return self.rows.get(notion_feed._key(page_id))

    async def _save_post(self, page_id, thread_id, *, status, priority, title):
        self.rows[notion_feed._key(page_id)] = notion_feed.Post(notion_feed._key(page_id), thread_id, status, priority, title)

    async def _delete_post(self, page_id):
        self.rows.pop(notion_feed._key(page_id), None)

    @contextlib.contextmanager
    def patched(self):
        names = {"user-1": "Vegeta", "user-2": "Bulma"}
        with patch.object(notion_feed, "get_post", self._get_post), \
                patch.object(notion_feed, "save_post", self._save_post), \
                patch.object(notion_feed, "delete_post", self._delete_post), \
                patch.object(notion, "get_page", AsyncMock(side_effect=lambda token, page_id: self.page)), \
                patch.object(notion, "get_comment", AsyncMock(side_effect=lambda token, comment_id: self.comment)), \
                patch.object(notion, "user_name", AsyncMock(side_effect=lambda token, user_id: names.get(user_id))):
            yield

    async def run(self, payload: dict) -> None:
        with self.patched():
            await self.cog.process(payload)

    def tag_names(self, thread: FakeThread) -> set[str]:
        return {tag.name for tag in thread.applied_tags}


class VerifyTests(unittest.TestCase):
    def test_good_bad_and_missing_signatures(self) -> None:
        body = b'{"type":"page.created"}'
        self.assertTrue(notion.verify_webhook("tok", body, sign("tok", body)))
        self.assertFalse(notion.verify_webhook("tok", body, sign("other", body)))
        self.assertFalse(notion.verify_webhook("tok", body + b" ", sign("tok", body)))
        self.assertFalse(notion.verify_webhook("tok", body, None))
        self.assertFalse(notion.verify_webhook(None, body, sign("tok", body)))


class ItemTests(unittest.TestCase):
    def test_item_from_page_reads_the_tracker_fields(self) -> None:
        item = notion_feed.item_from_page(page(priority="P0 Critical"))
        self.assertEqual(item["key"], "DMZ-12")
        self.assertEqual((item["title"], item["status"], item["project"], item["type"]), ("Ki blast crash", "Inbox", "Mod", "Bug"))
        self.assertEqual(item["assignees"], ["Goku"])
        self.assertTrue(notion_feed.same_id(item["parent"], DS))
        self.assertFalse(notion_feed.same_id(None, DS))
        self.assertEqual(notion_feed.tags_for(item), ["Mod", "Inbox", "P0"])
        self.assertEqual(notion_feed.tags_for(notion_feed.item_from_page(page(project="Elsewhere"))), ["Inbox"])

    def test_post_name_fits_discords_cap(self) -> None:
        item = notion_feed.item_from_page(page(title="x" * 200))
        name = notion_feed.post_name(item)
        self.assertEqual(len(name), 100)
        self.assertTrue(name.startswith("DMZ-12 · x"))


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_item_card_shows_the_item_without_pings(self) -> None:
        item = notion_feed.item_from_page(page(title="Crash @everyone <@123>", people=("Goku", "Vegeta")))
        text = "\n".join(texts(notion_cards.item_card(item)))
        self.assertIn("### [DMZ-12 · Crash", text)
        self.assertIn("(https://www.notion.so/x)", text)
        self.assertIn("**Status** Inbox", text)
        self.assertIn("**Assignees** Goku, Vegeta", text)
        self.assertIn("-# Notion Tracker", text)
        self.assertNotIn("@everyone", text)
        self.assertNotIn("<@123>", text)


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = Harness()
        self.loop = MagicMock()

    def test_handshake_logs_the_token_and_answers_200(self) -> None:
        body = json.dumps({"verification_token": "secret_abc"}).encode()
        with self.assertLogs("bulmaai.cogs.notion_feed", level="INFO") as logs:
            response = self.harness.cog.handle(body, {}, self.loop)
        self.assertEqual(response.status, 200)
        self.assertIn("secret_abc", logs.output[0])
        self.assertTrue(logs.records[0].levelname == "INFO")

    def test_bad_signature_is_401_and_missing_token_503(self) -> None:
        body = json.dumps(event()).encode()
        self.assertEqual(self.harness.cog.handle(body, {"X-Notion-Signature": sign("wrong", body)}, self.loop).status, 401)
        self.harness.cog.settings.notion_webhook_token = None
        self.assertEqual(self.harness.cog.handle(body, {"X-Notion-Signature": sign("whsec", body)}, self.loop).status, 503)

    def test_bad_json_is_400_and_unknown_events_are_ignored(self) -> None:
        self.assertEqual(self.harness.cog.handle(b"{nope", {}, self.loop).status, 400)
        body = json.dumps(event("database.schema_updated")).encode()
        response = self.harness.cog.handle(body, {"X-Notion-Signature": sign("whsec", body)}, self.loop)
        self.assertEqual(response.status, 200)

    def test_signed_event_is_queued_on_the_bot_loop(self) -> None:
        body = json.dumps(event()).encode()
        with patch("bulmaai.cogs.notion_feed.asyncio.run_coroutine_threadsafe") as run:
            response = self.harness.cog.handle(body, {"X-Notion-Signature": sign("whsec", body)}, self.loop)
        self.assertEqual(response.status, 200)
        run.assert_called_once()
        run.call_args.args[0].close()


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.h = Harness()

    async def _created(self) -> FakeThread:
        await self.h.run(event("page.created", event_id="ev-0"))
        return next(iter(self.h.threads.values()))

    async def test_new_item_creates_a_post_with_its_tags(self) -> None:
        thread = await self._created()
        self.assertEqual(thread.name, "DMZ-12 · Ki blast crash")
        self.assertEqual(self.h.tag_names(thread), {"Mod", "Inbox"})
        self.assertEqual(len(self.h.forum.available_tags), len(notion_feed.TAG_EMOJI))
        self.assertIn("**Status** Inbox", "\n".join(thread.card))
        self.assertEqual(self.h.rows[notion_feed._key(PAGE_ID)].thread_id, thread.id)

    async def test_existing_forum_tags_are_kept(self) -> None:
        own = discord.ForumTag(name="Help", emoji="❓")
        own.id = 77
        self.h.forum.available_tags = [own]
        await self._created()
        names = [tag.name for tag in self.h.forum.available_tags]
        self.assertEqual(names[0], "Help")
        self.assertEqual(len(names), len(notion_feed.TAG_EMOJI) + 1)

    async def test_status_change_notes_swaps_tags_and_edits_the_card(self) -> None:
        thread = await self._created()
        self.h.page = page(status="In progress", priority="P0 Critical")
        await self.h.run(event(event_id="ev-2"))
        self.assertIn("**Vegeta** moved it **Inbox → In progress**", thread.sent[0])
        self.assertIn("**Vegeta** set priority **P2 Normal → P0 Critical**", thread.sent[1])
        self.assertEqual(self.h.tag_names(thread), {"Mod", "In progress", "P0"})
        thread.starter.edit.assert_awaited_once()
        self.assertNotIn("name", thread.edits[-1])  # title unchanged: no rename
        self.assertEqual(self.h.rows[notion_feed._key(PAGE_ID)].status, "In progress")

    async def test_other_property_change_only_rerenders(self) -> None:
        thread = await self._created()
        self.h.page = page(people=("Goku", "Gohan"))
        await self.h.run(event(event_id="ev-2"))
        self.assertEqual(thread.sent, [])
        thread.starter.edit.assert_awaited_once()

    async def test_title_change_renames(self) -> None:
        thread = await self._created()
        self.h.page = page(title="Ki blast crash on servers")
        await self.h.run(event(event_id="ev-2"))
        self.assertEqual(thread.name, "DMZ-12 · Ki blast crash on servers")

    async def test_done_archives_and_moving_out_unarchives_first(self) -> None:
        thread = await self._created()
        self.h.page = page(status="Done")
        await self.h.run(event(event_id="ev-2"))
        self.assertTrue(thread.archived)
        self.assertFalse(thread.locked)
        self.assertIn("Inbox → Done", thread.sent[-1])
        self.h.page = page(status="Backlog")
        await self.h.run(event(event_id="ev-3"))
        self.assertEqual(thread.edits[-2], {"archived": False, "locked": False})
        self.assertFalse(thread.archived)
        self.assertIn("Done → Backlog", thread.sent[-1])

    async def test_untracked_done_item_gets_no_post(self) -> None:
        self.h.page = page(status="Won't do")
        await self.h.run(event(event_id="ev-1"))
        await self.h.run(event("page.created", event_id="ev-2"))
        self.h.forum.create_thread.assert_not_awaited()
        self.assertEqual(self.h.rows, {})

    async def test_untracked_open_item_gets_a_post_on_update(self) -> None:
        await self.h.run(event(event_id="ev-1"))
        self.h.forum.create_thread.assert_awaited_once()

    async def test_pages_outside_the_tracker_are_ignored(self) -> None:
        self.h.page = page(parent="99999999-2222-3333-4444-555555555555")
        await self.h.run(event("page.created"))
        self.h.forum.create_thread.assert_not_awaited()

    async def test_deleted_notes_archives_and_locks(self) -> None:
        thread = await self._created()
        self.h.page = page(trash=True)
        await self.h.run(event("page.deleted", event_id="ev-2"))
        self.assertIn("Deleted in Notion by **Vegeta**", thread.sent[-1])
        self.assertTrue(thread.archived and thread.locked)
        self.assertEqual(self.h.rows[notion_feed._key(PAGE_ID)].status, notion_feed.DELETED)
        self.h.page = page()
        await self.h.run(event("page.undeleted", event_id="ev-3"))
        self.assertFalse(thread.archived or thread.locked)
        self.assertEqual(len(thread.sent), 1)  # restoring posts no "Deleted → Inbox" note

    async def test_gone_thread_is_recreated(self) -> None:
        thread = await self._created()
        del self.h.threads[thread.id]
        self.h.page = page(status="In progress")
        await self.h.run(event(event_id="ev-2"))
        self.assertEqual(self.h.forum.create_thread.await_count, 2)
        self.assertNotEqual(self.h.rows[notion_feed._key(PAGE_ID)].thread_id, thread.id)

    async def test_comment_on_tracked_item_is_posted(self) -> None:
        thread = await self._created()
        await self.h.run(event("comment.created", event_id="ev-2", entity="c-1", data={"page_id": PAGE_ID}))
        self.assertEqual(thread.sent, ["💬 **Bulma**: Looks fixed on main"])

    async def test_comment_on_done_item_rearchives(self) -> None:
        thread = await self._created()
        self.h.page = page(status="Done")
        await self.h.run(event(event_id="ev-2"))
        await self.h.run(event("comment.created", event_id="ev-3", entity="c-1", data={"page_id": PAGE_ID}))
        self.assertTrue(thread.archived)
        self.assertIn("Looks fixed", thread.sent[-1])

    async def test_bot_comments_and_untracked_pages_are_skipped(self) -> None:
        thread = await self._created()
        self.h.comment = {**self.h.comment, "author_type": "bot"}
        await self.h.run(event("comment.created", event_id="ev-2", entity="c-1", data={"page_id": PAGE_ID}))
        await self.h.run(event("comment.created", event_id="ev-3", entity="c-2", data={"page_id": "other"}))
        self.assertEqual(thread.sent, [])

    async def test_duplicate_event_is_processed_once(self) -> None:
        await self.h.run(event("page.created", event_id="same"))
        await self.h.run(event("page.created", event_id="same"))
        self.h.forum.create_thread.assert_awaited_once()

    async def test_events_run_one_at_a_time(self) -> None:
        with self.h.patched():
            await asyncio.gather(self.h.cog.process(event("page.created", event_id="a")), self.h.cog.process(event(event_id="b")))
        self.h.forum.create_thread.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
