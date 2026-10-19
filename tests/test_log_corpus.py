import asyncio
import json
import os
import tempfile
import time
import unittest
import zipfile
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import log_corpus as cog_module
from bulmaai.cogs import log_parser
from bulmaai.cogs.log_corpus import LogCorpusCog
from bulmaai.cogs.log_parser import LogParserCog
from bulmaai.services import log_corpus as corpus
from bulmaai.services.moderation import ModerationState
from bulmaai.ui import log_corpus_views as views
from bulmaai.ui.v2 import WORKING
from bulmaai.utils.log_parser import parse_log
from bulmaai.utils.permissions import BRUNO_ID
from v2_helpers import buttons, sent_message, text, walk

GUILD = 1
OTHER_GUILD = 2
SOURCE_CHANNEL = 50
CORPUS_CHANNEL = 60
TICKETS_CATEGORY = 70
STAFF_ROLE = 7
HELPER_ROLE = 8
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

UPLOADER_ID = 111111111111111111
STAFF_ID = 222222222222222222
MEMBER_ID = 333333333333333333
BOT_ID = 444444444444444444
MENTIONED_USER = 555555555555555555
MENTIONED_ROLE = 666666666666666666
MENTIONED_CHANNEL = 777777777777777777

LOG = (
    "[12:00:00] [main/INFO]: Setting user: SonGoku99\n"
    "[12:00:00] [main/INFO]: ModLauncher running: args [--fml.forgeVersion, 47.4.10, --fml.mcVersion, 1.20.1]\n"
    "[12:00:01] [main/INFO]: Loading mods/dragonminez-2.2.0.jar\n"
    "[12:00:02] [Server thread/INFO]: SonGoku99[/203.0.113.7:51234] logged in with entity id 7\n"
    "[12:00:05] [Render thread/ERROR]: Failed to load C:\\Users\\Bruno Silva\\mods\\geckolib.jar\n"
    "java.lang.IllegalStateException: broken for SonGoku99\n"
    "\tat com.example.Foo.bar(Foo.java:10)\n"
    "Caused by: java.lang.NullPointerException: null\n"
)


def settings(**overrides):
    values = dict(
        log_corpus_channel_id=CORPUS_CHANNEL,
        log_corpus_ping_threshold=2,
        panel_guild_id=GUILD,
        dev_guild_id=None,
        discord_staff_role_ids=(STAFF_ROLE,),
        panel_owner_role_ids=(),
        panel_admin_role_ids=(),
        panel_moderator_role_ids=(),
        panel_helper_role_ids=(HELPER_ROLE,),
        ai_ticket_category_id=TICKETS_CATEGORY,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def person(user_id, *roles, bot=False, admin=False, guild_id=GUILD):
    return SimpleNamespace(
        id=user_id,
        bot=bot,
        roles=[SimpleNamespace(id=role) for role in roles],
        guild=SimpleNamespace(id=guild_id),
        guild_permissions=SimpleNamespace(administrator=admin),
    )


UPLOADER = person(UPLOADER_ID)
STAFF = person(STAFF_ID, STAFF_ROLE)
MEMBER = person(MEMBER_ID)
BOT = person(BOT_ID, bot=True)
ADMIN = person(20, admin=True)
BRUNO = person(BRUNO_ID)


def http_error(cls=discord.HTTPException, status=500):
    return cls(MagicMock(status=status, reason="x"), "boom")


class FakeSourceChannel:
    def __init__(self, channel_id=SOURCE_CHANNEL, messages=(), category_id=None):
        self.id = channel_id
        self.category_id = category_id
        self.messages = list(messages)

    def history(self, *, limit, before=None, after=None, oldest_first=None):
        def at(value):
            return getattr(value, "created_at", value)

        items = [
            m for m in self.messages
            if (before is None or m.created_at < at(before)) and (after is None or m.created_at > at(after))
        ]
        items.sort(key=lambda m: m.created_at, reverse=not oldest_first)

        async def gen():
            for m in items[:limit]:
                yield m

        return gen()

    async def fetch_message(self, message_id):
        for m in self.messages:
            if m.id == message_id:
                return m
        raise http_error(discord.NotFound, 404)

    def typing(self):
        return nullcontext()


def chat(message_id, author, content, at, *, channel=None, reply_to=None, guild_id=GUILD):
    return SimpleNamespace(
        id=message_id,
        author=author,
        content=content,
        created_at=at,
        reference=SimpleNamespace(message_id=reply_to) if reply_to else None,
        guild=SimpleNamespace(id=guild_id, filesize_limit=25 * 1024 * 1024),
        channel=channel,
    )


class FakeCorpusChannel:
    """Records what the bot posts; reads and closes any uploaded files like Discord's send does."""

    def __init__(self):
        self.sent = []
        self.calls = 0
        self.fail_on: int | None = None
        self._next_id = 9000

    async def send(self, *, view=None, file=None, files=None, allowed_mentions=None):
        self.calls += 1
        uploads = [*(files or []), *([file] if file else [])]
        try:
            if self.fail_on == self.calls:
                raise http_error()
            payload = {upload.filename: upload.fp.read() for upload in uploads}
        finally:
            for upload in uploads:
                upload.close()
        self._next_id += 1
        message = SimpleNamespace(
            id=self._next_id, view=view, files=payload, allowed_mentions=allowed_mentions, delete=AsyncMock()
        )
        self.sent.append(message)
        return message


class FakeStore:
    """The DB half of services/log_corpus in memory."""

    def __init__(self):
        self.rows: dict[tuple[int, str], corpus.Entry] = {}
        self.pings: set[str] = set()

    async def insert_entry(self, message_id, filename, channel_id, guild_id, dmz_version):
        if (message_id, filename) in self.rows:
            return False
        self.rows[(message_id, filename)] = corpus.Entry(
            message_id, filename, channel_id, guild_id, corpus.version_key(dmz_version), None, False,
            NOW + timedelta(seconds=len(self.rows)), None,
        )
        return True

    async def delete_entry(self, message_id, filename):
        self.rows.pop((message_id, filename), None)

    async def set_notice(self, message_id, filename, notice_message_id):
        key = (message_id, filename)
        self.rows[key] = replace(self.rows[key], notice_message_id=notice_message_id)

    async def find_entry(self, message_id, key):
        return next((e for e in self.rows.values() if e.message_id == message_id and e.key == key), None)

    async def set_excluded(self, message_id, filename, excluded):
        entry = self.rows.get((message_id, filename))
        if entry is None or entry.bundled_at is not None:
            return None
        self.rows[(message_id, filename)] = entry = replace(entry, excluded=excluded)
        return entry

    def _pending(self, version=None):
        return sorted(
            (e for e in self.rows.values()
             if e.bundled_at is None and not e.excluded and (version is None or e.dmz_version == version)),
            key=lambda e: e.created_at,
        )

    async def pending_count(self, version):
        return len(self._pending(version))

    async def pending_entries(self, version=None):
        return self._pending(version)

    async def claim_ping(self, version):
        if version in self.pings:
            return False
        self.pings.add(version)
        return True

    async def clear_pings(self, versions):
        self.pings -= set(versions)

    async def mark_bundled(self, entries):
        for e in entries:
            self.rows[(e.message_id, e.filename)] = replace(e, bundled_at=NOW)

    def patch(self):
        return patch.multiple(
            corpus,
            **{name: getattr(self, name) for name in (
                "insert_entry", "delete_entry", "set_notice", "find_entry", "set_excluded", "pending_count",
                "pending_entries", "claim_ping", "clear_pings", "mark_bundled",
            )},
        )


class CorpusTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "log_corpus"
        for patcher in (patch.object(corpus, "CORPUS_DIR", self.dir), (store := FakeStore()).patch()):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = store
        self.settings = settings()
        self.corpus_channel = FakeCorpusChannel()
        self.source = FakeSourceChannel()
        self.bot = SimpleNamespace(
            settings=self.settings,
            get_partial_messageable=lambda channel_id: self.corpus_channel,
            get_channel=lambda channel_id: self.source if channel_id == self.source.id else None,
            fetch_channel=AsyncMock(side_effect=http_error(discord.NotFound, 404)),
        )
        self.cog = LogCorpusCog(self.bot)

    def log_message(self, message_id=1000, *, at=NOW, content="crash pls, I'm SonGoku99, mail goku@capsule.example.com",
                    guild_id=GUILD, author=UPLOADER):
        message = chat(message_id, author, content, at, channel=self.source, guild_id=guild_id)
        self.source.messages.append(message)
        return message

    def chat_around(self, log_id=1000):
        self.source.messages += [
            chat(1, MEMBER, "too old", NOW - timedelta(minutes=40)),
            chat(2, MEMBER, f"crashed again <@{MENTIONED_USER}> <@&{MENTIONED_ROLE}> see <#{MENTIONED_CHANNEL}>",
                 NOW - timedelta(minutes=20)),
            chat(3, STAFF, "send your latest.log @everyone", NOW - timedelta(minutes=10)),
            chat(4, BOT, "bot noise", NOW - timedelta(minutes=5)),
            chat(5, UPLOADER, "here it is C:\\Users\\Bruno Silva\\logs", NOW - timedelta(minutes=2)),
            chat(2001, STAFF, "update GeckoLib, SonGoku99", NOW + timedelta(minutes=5), reply_to=log_id),
            chat(2002, MEMBER, "same here", NOW + timedelta(hours=1)),
            chat(2003, BOT, "bot noise", NOW + timedelta(hours=2)),
            chat(2004, UPLOADER, "much later", NOW + timedelta(hours=50)),
        ]

    async def collect(self, message=None, *, log=LOG, filename="latest.log", channel_kind="public"):
        message = message or self.log_message()
        return await self.cog.collect(message, log, filename, parse_log(log), channel_kind=channel_kind)

    def click(self, user, custom_id, card):
        interaction = MagicMock()
        interaction.type = discord.InteractionType.component
        interaction.data = {"custom_id": custom_id}
        interaction.user = user
        interaction.guild = SimpleNamespace(filesize_limit=25 * 1024 * 1024)
        interaction.message = sent_message(card, message_id=9500, channel_id=CORPUS_CHANNEL)
        interaction.response.send_message = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction


class ScrubTests(unittest.TestCase):
    def test_mentions_become_labels_and_personal_details_are_masked(self):
        scrubbed = corpus.scrub(
            "hi <@123> <@!456> <@&789> in <#321> @everyone C:\\Users\\Bruno Silva\\x goku@capsule.example.com Vegeta77",
            names={"Vegeta77"},
        )
        self.assertEqual(scrubbed.count("@user"), 2)
        self.assertIn("@role", scrubbed)
        self.assertIn("#channel", scrubbed)
        self.assertNotIn("<@", scrubbed)
        self.assertNotIn("<#", scrubbed)
        self.assertNotIn("@everyone", scrubbed)  # defused with a zero-width space
        self.assertIn("C:\\Users\\<user>", scrubbed)
        self.assertIn("<email>", scrubbed)
        self.assertIn("<player>", scrubbed)
        for digits in ("123", "456", "789", "321"):
            self.assertNotIn(digits, scrubbed)

    def test_hidden_names_mask_later_chat_without_keeping_the_name(self):
        hidden = corpus.hide_names({"SonGoku99"})
        self.assertNotIn("SonGoku99", json.dumps(hidden))
        self.assertEqual(
            corpus.mask_hidden_names("thanks songoku99, SonGoku990 is fine", hidden), "thanks <player>, SonGoku990 is fine"
        )
        self.assertEqual(corpus.mask_hidden_names("SonGoku99", None), "SonGoku99")

    def test_who_labels_uploader_staff_and_members(self):
        s = settings()
        self.assertEqual(corpus.who(UPLOADER, UPLOADER_ID, s), "uploader")
        self.assertEqual(corpus.who(STAFF, UPLOADER_ID, s), "staff")
        self.assertEqual(corpus.who(person(9, HELPER_ROLE), UPLOADER_ID, s), "staff")
        self.assertEqual(corpus.who(MEMBER, UPLOADER_ID, s), "member")

    def test_stored_file_name_is_safe_and_nameless(self):
        self.assertEqual(corpus.safe_filename("SonGoku99 crash (1).txt", {"SonGoku99"}), "player_crash_1_.txt")
        self.assertEqual(corpus.safe_filename("../../etc/passwd"), "etc_passwd")

    def test_custom_ids_round_trip(self):
        self.assertEqual(views.parse_custom_id(views.toggle_custom_id(views.EXCLUDE, 12, "abc")), ("exclude", 12, "abc"))
        self.assertEqual(views.parse_custom_id(views.zip_custom_id("2.2.0-beta")), ("zip", None, "2.2.0-beta"))
        for bad in ("logcorpus:exclude:x:abc", "logcorpus:delete:1:abc", "other:zip:1", "logcorpus:zip:"):
            self.assertIsNone(views.parse_custom_id(bad))


class CollectTests(CorpusTestCase):
    async def test_saves_the_redacted_log_and_the_chat_before_it(self):
        self.chat_around()
        message = self.log_message()

        self.assertTrue(await self.collect(message))

        saved = corpus.log_path(1000, "latest.log").read_text(encoding="utf-8")
        for secret in ("SonGoku99", "Bruno Silva", "203.0.113.7"):
            self.assertNotIn(secret, saved)
        self.assertIn("Setting user: <player>", saved)
        context = corpus.read_context(1000, "latest.log")
        self.assertEqual(
            (context["dmz_version"], context["minecraft_version"], context["forge_version"], context["channel_kind"]),
            ("2.2.0", "1.20.1", "47.4.10", "public"),
        )
        self.assertEqual(context["error_count"], 1)
        self.assertEqual(context["posted_at"], NOW.isoformat())
        self.assertEqual(context["uploader_text"], "crash pls, I'm <player>, mail <email>")
        before = context["before"]
        self.assertEqual([m["who"] for m in before], ["member", "staff", "uploader"])
        self.assertEqual(before[0]["text"], "crashed again @user @role see #channel")
        self.assertNotIn("@everyone", before[1]["text"])
        self.assertEqual(before[2]["text"], "here it is C:\\Users\\<user>\\logs")
        raw = corpus.context_path(1000, "latest.log").read_text(encoding="utf-8")
        for leak in (UPLOADER_ID, STAFF_ID, MEMBER_ID, MENTIONED_USER, MENTIONED_ROLE, MENTIONED_CHANNEL, "SonGoku99"):
            self.assertNotIn(str(leak), raw)
        self.assertEqual(self.store.rows[(1000, "latest.log")].dmz_version, "2.2.0")

    async def test_notice_card_shows_the_facts_the_cause_and_the_file(self):
        await self.collect()

        notice = self.corpus_channel.sent[0]
        mentions = notice.allowed_mentions
        self.assertEqual((mentions.everyone, mentions.users, mentions.roles, mentions.replied_user), (False, False, False, False))
        body = text(notice.view)
        self.assertIn(views.NOTICE_HEAD, body)
        self.assertIn("**DMZ** `2.2.0`", body)
        self.assertIn("**Minecraft** `1.20.1`", body)
        self.assertIn("**Forge** `47.4.10`", body)
        self.assertIn("**File** `latest.log`", body)
        self.assertIn("**Errors** 1", body)
        self.assertIn("🎯 `NullPointerException: null`", body)
        jump, toggle = buttons(notice.view)
        self.assertEqual((jump.label, jump.url), ("Jump to message", f"https://discord.com/channels/{GUILD}/{SOURCE_CHANNEL}/1000"))
        self.assertEqual(toggle.label, "Exclude")
        self.assertEqual(toggle.custom_id, views.toggle_custom_id("exclude", 1000, corpus.file_key("latest.log")))
        self.assertIn("1000-latest.log", notice.files)
        self.assertNotIn(b"SonGoku99", notice.files["1000-latest.log"])
        file_items = [item for item in walk(notice.view) if isinstance(item, discord.ui.File)]
        self.assertEqual([item.url for item in file_items], ["attachment://1000-latest.log"])
        self.assertEqual(self.store.rows[(1000, "latest.log")].notice_message_id, notice.id)

    def test_cause_line_names_the_culprit(self):
        line = views.cause_line(
            {"culprit_mod": "noeabosses", "culprit_jar": "Noea.jar", "exception": "InjectionError: <@1> https://x.y", "kind": "mixin"}
        )
        self.assertTrue(line.startswith("🎯 Mixin injection failed in `noeabosses` (`Noea.jar`): `InjectionError: "))
        self.assertNotIn("<@1>", line)
        self.assertNotIn("https://", line)
        self.assertIsNone(views.cause_line(None))

    async def test_the_same_message_and_file_twice_is_skipped_silently(self):
        message = self.log_message()
        self.assertTrue(await self.collect(message))
        self.assertFalse(await self.collect(message))
        self.assertEqual(len(self.corpus_channel.sent), 1)
        self.assertEqual(len(self.store.rows), 1)

    async def test_skipped_outside_the_main_guild(self):
        message = self.log_message(guild_id=OTHER_GUILD)
        self.assertFalse(await self.collect(message))
        self.cog.collect_later(message, LOG, "latest.log", parse_log(LOG), channel_kind="public")
        self.assertEqual(self.cog._tasks, set())
        self.assertEqual((self.store.rows, self.corpus_channel.sent), ({}, []))
        self.assertFalse(self.dir.exists())

    async def test_skipped_when_the_channel_setting_is_none(self):
        self.settings.log_corpus_channel_id = None
        message = self.log_message()
        self.assertFalse(await self.collect(message))
        self.cog.collect_later(message, LOG, "latest.log", parse_log(LOG), channel_kind="public")
        self.assertEqual(self.cog._tasks, set())
        self.assertEqual((self.store.rows, self.corpus_channel.sent), ({}, []))

    async def test_a_failed_write_leaves_no_row(self):
        with patch.object(corpus, "write_files", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.collect()
        self.assertEqual((self.store.rows, self.corpus_channel.sent), ({}, []))

    async def test_a_failed_notice_still_keeps_the_entry(self):
        self.corpus_channel.fail_on = 1
        self.assertTrue(await self.collect())
        self.assertIn((1000, "latest.log"), self.store.rows)

    async def test_collect_later_logs_failures(self):
        with patch.object(self.cog, "collect", AsyncMock(side_effect=RuntimeError("db down"))):
            with self.assertLogs("bulmaai.cogs.log_corpus", "ERROR"):
                self.cog.collect_later(self.log_message(), LOG, "latest.log", parse_log(LOG), channel_kind="public")
                for _ in range(3):
                    await asyncio.sleep(0)
        self.assertEqual(self.cog._tasks, set())


class ToggleTests(CorpusTestCase):
    async def asyncSetUp(self):
        await self.collect()
        self.notice = self.corpus_channel.sent[0]
        self.key = corpus.file_key("latest.log")

    async def toggle(self, user, action):
        interaction = self.click(user, views.toggle_custom_id(action, 1000, self.key), self.notice.view)
        await self.cog.on_interaction(interaction)
        edit = interaction.response.edit_message
        if edit.await_count:
            for upload in edit.await_args.kwargs["files"]:
                upload.close()
        return interaction

    async def test_only_admins_can_exclude(self):
        for user in (STAFF, BRUNO, person(21, admin=True, guild_id=OTHER_GUILD)):
            interaction = await self.toggle(user, views.EXCLUDE)
            interaction.response.send_message.assert_awaited_once_with(cog_module.ADMIN_ONLY_MSG, ephemeral=True)
            interaction.response.edit_message.assert_not_awaited()
        self.assertFalse(self.store.rows[(1000, "latest.log")].excluded)

    async def test_exclude_then_include(self):
        interaction = await self.toggle(ADMIN, views.EXCLUDE)

        self.assertTrue(self.store.rows[(1000, "latest.log")].excluded)
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertEqual(kwargs["attachments"], [])
        self.assertEqual([upload.filename for upload in kwargs["files"]], ["1000-latest.log"])
        body = text(kwargs["view"])
        self.assertIn(views.EXCLUDED_LINE, body)
        self.assertIn("**DMZ** `2.2.0`", body)
        toggle = buttons(kwargs["view"])[-1]
        self.assertEqual((toggle.label, toggle.custom_id), ("Include", views.toggle_custom_id("include", 1000, self.key)))

        interaction = await self.toggle(ADMIN, views.INCLUDE)

        self.assertFalse(self.store.rows[(1000, "latest.log")].excluded)
        view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertNotIn(views.EXCLUDED_LINE, text(view))
        self.assertEqual(buttons(view)[-1].label, "Exclude")

    async def test_a_bundled_log_cannot_be_toggled(self):
        await self.cog.bundle("2.2.0")
        interaction = await self.toggle(ADMIN, views.EXCLUDE)
        interaction.response.send_message.assert_awaited_once_with(cog_module.GONE_MSG, ephemeral=True)


class PingTests(CorpusTestCase):
    async def test_pings_bruno_once_when_a_version_reaches_the_threshold(self):
        await self.collect(self.log_message(1000))
        self.assertEqual(len(self.corpus_channel.sent), 1)  # just the notice

        await self.collect(self.log_message(1001))

        ping = self.corpus_channel.sent[-1]
        self.assertIn(f"<@{BRUNO_ID}> **2 logs collected for DragonMineZ 2.2.0**", text(ping.view))
        mentions = ping.allowed_mentions
        self.assertIsInstance(mentions.users, list)
        self.assertEqual([user.id for user in mentions.users], [BRUNO_ID])
        self.assertEqual((mentions.everyone, mentions.roles, mentions.replied_user), (False, False, False))
        (download,) = buttons(ping.view)
        self.assertEqual((download.label, download.custom_id), ("Download .zip", "logcorpus:zip:2.2.0"))

        await self.collect(self.log_message(1002))
        pings = [m for m in self.corpus_channel.sent if "logs collected" in text(m.view)]
        self.assertEqual(len(pings), 1)

    async def test_notices_never_ping(self):
        for i in range(3):
            await self.collect(self.log_message(1000 + i))
        for message in self.corpus_channel.sent:
            if "logs collected" not in text(message.view):
                self.assertFalse(message.allowed_mentions.users)

    async def test_zero_threshold_never_pings(self):
        self.settings.log_corpus_ping_threshold = 0
        for i in range(3):
            await self.collect(self.log_message(1000 + i))
        self.assertFalse(any("logs collected" in text(m.view) for m in self.corpus_channel.sent))

    async def test_a_bundle_re_arms_the_ping(self):
        await self.collect(self.log_message(1000))
        await self.collect(self.log_message(1001))
        self.assertEqual(self.store.pings, {"2.2.0"})
        await self.cog.bundle("2.2.0")
        self.assertEqual(self.store.pings, set())


class BundleTests(CorpusTestCase):
    async def asyncSetUp(self):
        self.chat_around(1000)
        await self.collect(self.log_message(1000))
        await self.collect(self.log_message(1001, at=NOW + timedelta(minutes=1)))

    def zip_messages(self):
        return [m for m in self.corpus_channel.sent if any(name.endswith(".zip") for name in m.files)]

    @staticmethod
    def open_zip(message):
        (name, data), = message.files.items()
        path = Path(tempfile.mkdtemp()) / name
        path.write_bytes(data)
        return name, zipfile.ZipFile(path)

    async def test_download_button_posts_the_zip_and_cleans_up(self):
        ping = views.ping_card(2, "2.2.0", BRUNO_ID)
        interaction = self.click(BRUNO, views.zip_custom_id("2.2.0"), ping)

        await self.cog.on_interaction(interaction)

        busy_view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(buttons(busy_view)[0].label, WORKING)
        (posted,) = self.zip_messages()
        self.assertEqual(posted.allowed_mentions.users, False)
        self.assertIn("### 📦 Log corpus: DragonMineZ 2.2.0", text(posted.view))
        self.assertIn("**Logs** 2", text(posted.view))
        name, archive = self.open_zip(posted)
        self.assertEqual(name, f"dmz-logs-2.2.0-{datetime.now(timezone.utc):%Y%m%d}.zip")
        self.assertEqual(
            sorted(archive.namelist()),
            ["2.2.0/1000-latest.log", "2.2.0/1000-latest.log.json", "2.2.0/1001-latest.log",
             "2.2.0/1001-latest.log.json", "README.txt"],
        )
        self.assertEqual(archive.read("README.txt").decode(), corpus.README)
        self.assertNotIn(b"SonGoku99", archive.read("2.2.0/1000-latest.log"))
        context = json.loads(archive.read("2.2.0/1000-latest.log.json"))
        self.assertTrue(context["after_available"])
        after = context["after"]
        # The uploader's second log (1001) counts as chat after the first; bots and the 50h-later message don't.
        self.assertEqual([m["who"] for m in after], ["uploader", "staff", "member"])
        self.assertEqual([m["is_reply_to_log"] for m in after], [False, True, False])
        self.assertEqual(after[1]["at"], (NOW + timedelta(minutes=5)).isoformat())
        self.assertEqual(after[1]["text"], "update GeckoLib, <player>")  # masked through the stored name hashes
        self.assertNotIn(corpus.HIDDEN_NAMES_KEY, context)
        self.assertEqual([m["who"] for m in context["before"]], ["member", "staff", "uploader"])
        self.assertEqual((context["dmz_version"], context["file"], context["channel_kind"]), ("2.2.0", "latest.log", "public"))
        self.assertIsNotNone(context["root_cause"])
        raw = archive.read("2.2.0/1000-latest.log.json").decode()
        for leak in (UPLOADER_ID, STAFF_ID, MEMBER_ID):
            self.assertNotIn(str(leak), raw)
        archive.close()

        self.assertTrue(all(row.bundled_at is not None for row in self.store.rows.values()))
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertEqual(self.store.pings, set())
        done = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIn("✅ 2 logs posted as a .zip.", text(done))
        self.assertEqual(buttons(done), [])
        self.assertNotIn(f"<@{BRUNO_ID}>", text(done))
        interaction.followup.send.assert_not_awaited()

    async def test_after_context_is_empty_when_the_log_message_is_gone(self):
        self.source.messages = [m for m in self.source.messages if m.id != 1000]
        await self.cog.bundle("2.2.0")
        _name, archive = self.open_zip(self.zip_messages()[0])
        context = json.loads(archive.read("2.2.0/1000-latest.log.json"))
        archive.close()
        self.assertEqual((context["after"], context["after_available"]), ([], False))

    async def test_excluded_logs_stay_out(self):
        key = (1001, "latest.log")
        self.store.rows[key] = replace(self.store.rows[key], excluded=True)
        self.assertEqual(await self.cog.bundle("2.2.0"), (1, 1))
        _name, archive = self.open_zip(self.zip_messages()[0])
        self.assertNotIn("2.2.0/1001-latest.log", archive.namelist())
        archive.close()
        self.assertIsNone(self.store.rows[key].bundled_at)
        self.assertTrue(corpus.log_path(1001, "latest.log").exists())

    async def test_splits_into_parts_by_size(self):
        big = LOG + "".join(f"[12:00:{i % 60:02d}] [main/INFO]: {os.urandom(48).hex()}\n" for i in range(4000))
        await self.collect(self.log_message(1002, at=NOW + timedelta(minutes=2)), log=big)
        await self.collect(self.log_message(1003, at=NOW + timedelta(minutes=3)), log=big)
        limit = corpus.ZIP_MARGIN + 450_000
        with patch.object(self.cog, "_filesize_limit", return_value=limit):
            logs, zips = await self.cog.bundle("2.2.0")

        self.assertEqual((logs, zips), (4, 2))
        posted = self.zip_messages()
        names = [next(iter(m.files)) for m in posted]
        self.assertTrue(names[0].endswith("-part1.zip") and names[1].endswith("-part2.zip"))
        self.assertIn("**Part** 1/2", text(posted[0].view))
        members = []
        for message in posted:
            (data,) = message.files.values()
            self.assertLessEqual(len(data), limit)
            _name, archive = self.open_zip(message)
            self.assertIn("README.txt", archive.namelist())
            members += [n for n in archive.namelist() if n != "README.txt"]
            archive.close()
        self.assertEqual(len(members), 8)

    async def test_a_failed_upload_marks_nothing(self):
        big = LOG + "".join(f"[12:00:{i % 60:02d}] [main/INFO]: {os.urandom(48).hex()}\n" for i in range(4000))
        await self.collect(self.log_message(1002, at=NOW + timedelta(minutes=2)), log=big)
        await self.collect(self.log_message(1003, at=NOW + timedelta(minutes=3)), log=big)
        self.corpus_channel.fail_on = self.corpus_channel.calls + 2  # the second part
        pings = set(self.store.pings)
        ping = views.ping_card(4, "2.2.0", BRUNO_ID)
        interaction = self.click(ADMIN, views.zip_custom_id("2.2.0"), ping)

        with patch.object(self.cog, "_filesize_limit", return_value=corpus.ZIP_MARGIN + 450_000):
            await self.cog.on_interaction(interaction)

        interaction.followup.send.assert_awaited_once_with(cog_module.DOWNLOAD_FAILED_MSG, ephemeral=True)
        (first_part,) = self.zip_messages()
        first_part.delete.assert_awaited_once()
        self.assertTrue(all(row.bundled_at is None for row in self.store.rows.values()))
        self.assertEqual(len(list(self.dir.iterdir())), 8)
        self.assertEqual(self.store.pings, pings)

    async def test_members_cannot_download(self):
        interaction = self.click(STAFF, views.zip_custom_id("2.2.0"), views.ping_card(2, "2.2.0", BRUNO_ID))
        await self.cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(cog_module.DOWNLOAD_DENIED_MSG, ephemeral=True)
        self.assertEqual(self.zip_messages(), [])

    async def test_nothing_left_collapses_the_ping(self):
        await self.cog.bundle(None)
        interaction = self.click(BRUNO, views.zip_custom_id("2.2.0"), views.ping_card(2, "2.2.0", BRUNO_ID))
        await self.cog.on_interaction(interaction)
        self.assertIn("✅ Already downloaded.", text(interaction.edit_original_response.await_args.kwargs["view"]))
        interaction.followup.send.assert_awaited_once_with("No logs are waiting for 2.2.0.", ephemeral=True)

    async def test_command_is_bruno_only(self):
        ctx = MagicMock(author=ADMIN, respond=AsyncMock(), defer=AsyncMock())
        await self.cog.log_corpus_command.callback(self.cog, ctx, None)
        ctx.respond.assert_awaited_once_with(cog_module.BRUNO_ONLY_MSG, ephemeral=True)
        self.assertEqual(self.zip_messages(), [])

    async def test_command_bundles_every_version(self):
        await self.collect(self.log_message(1002), log=LOG.replace("2.2.0", "2.1.3"))
        ctx = MagicMock(author=BRUNO, respond=AsyncMock(), defer=AsyncMock())
        ctx.followup.send = AsyncMock()

        await self.cog.log_corpus_command.callback(self.cog, ctx, None)

        ctx.defer.assert_awaited_once_with(ephemeral=True)
        ctx.followup.send.assert_awaited_once_with(f"Posted 3 logs in 1 .zip file(s) in <#{CORPUS_CHANNEL}>.", ephemeral=True)
        name, archive = self.open_zip(self.zip_messages()[0])
        self.assertTrue(name.startswith("dmz-logs-all-"))
        self.assertIn("2.1.3/1002-latest.log", archive.namelist())
        archive.close()

    async def test_command_failure_is_an_ephemeral_error(self):
        self.corpus_channel.fail_on = self.corpus_channel.calls + 1
        ctx = MagicMock(author=BRUNO, respond=AsyncMock(), defer=AsyncMock())
        ctx.followup.send = AsyncMock()
        await self.cog.log_corpus_command.callback(self.cog, ctx, "2.2.0")
        ctx.followup.send.assert_awaited_once_with(cog_module.DOWNLOAD_FAILED_MSG, ephemeral=True)
        self.assertTrue(all(row.bundled_at is None for row in self.store.rows.values()))


class ZipTests(unittest.TestCase):
    def test_plan_parts_keeps_order_and_isolates_oversized_items(self):
        small = corpus.BundleItem("v", "1-a.log", b"a" * 100, b"{}")
        huge = corpus.BundleItem("v", "2-b.log", os.urandom(400_000), b"{}")
        parts = corpus.plan_parts([small, huge, small], corpus.ZIP_MARGIN + 300_000)
        self.assertEqual([[item.stem for item in part] for part in parts], [["1-a.log"], ["2-b.log"], ["1-a.log"]])
        self.assertEqual(len(corpus.plan_parts([small, small, small], corpus.ZIP_MARGIN + 300_000)), 1)


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_purge_deletes_old_rows_files_and_stray_files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        with patch.object(corpus, "CORPUS_DIR", directory):
            corpus.write_files(1, "old.log", "x", {})
            corpus.write_files(3, "new.log", "y", {})
            stray = directory / "9-stray.log"
            stray.write_text("z")
            old = time.time() - (corpus.RETENTION_DAYS + 1) * 86400
            os.utime(stray, (old, old))
            pool = SimpleNamespace(
                fetch=AsyncMock(return_value=[
                    {"message_id": 1, "filename": "old.log", "bundled_at": None},
                    {"message_id": 2, "filename": "gone.log", "bundled_at": NOW},
                ]),
                execute=AsyncMock(),
            )
            with patch.object(corpus, "get_pool", AsyncMock(return_value=pool)):
                self.assertEqual(await corpus.purge_expired(), 2)

            sql, days = pool.fetch.await_args.args
            self.assertIn("DELETE FROM log_corpus_entries", sql)
            self.assertEqual(days, 60)
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ["3-new.log", "3-new.log.json"])
            self.assertIn("DELETE FROM log_corpus_pings", pool.execute.await_args.args[0])

    async def test_hourly_sweep_runs_the_corpus_purge_and_survives_its_failure(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cog = LogParserCog.__new__(LogParserCog)
        cog.bot = SimpleNamespace(settings=SimpleNamespace(log_page_dir=tmp.name))
        for outcome in (AsyncMock(return_value=3), AsyncMock(side_effect=RuntimeError("db down"))):
            with (
                patch.object(log_parser.log_ai_quota, "purge", AsyncMock()),
                patch.object(log_parser.log_corpus, "purge_expired", outcome),
            ):
                await LogParserCog.purge_log_pages.coro(cog)
            outcome.assert_awaited_once()


class HookTests(unittest.IsolatedAsyncioTestCase):
    def make_parser(self, collector):
        cog = LogParserCog.__new__(LogParserCog)  # skips __init__, which starts the purge loop
        cog._pending = {}
        cog._parse_state = ModerationState()
        cog._parse_events = defaultdict(list)
        cog.bot = SimpleNamespace(settings=settings(), get_cog=lambda name: collector if name == "LogCorpusCog" else None)
        cog._analyze = AsyncMock(return_value=(parse_log(LOG), None))
        cog._add_diagnosis = AsyncMock()
        return cog

    def upload(self, category_id=None):
        attachment = SimpleNamespace(
            filename="latest.log", size=len(LOG), url="https://cdn/latest.log", read=AsyncMock(return_value=LOG.encode())
        )
        channel = FakeSourceChannel(category_id=category_id)
        message = chat(1000, UPLOADER, "help", NOW, channel=channel)
        message.attachments = [attachment]
        message.reply = AsyncMock(return_value=SimpleNamespace(id=5))
        return message

    async def test_card_is_posted_even_when_the_collector_raises(self):
        collector = SimpleNamespace(collect_later=MagicMock(side_effect=RuntimeError("boom")))
        cog = self.make_parser(collector)
        message = self.upload()

        with self.assertLogs("bulmaai.cogs.log_parser", "ERROR"):
            await cog.on_message(message)

        message.reply.assert_awaited_once()
        cog._add_diagnosis.assert_awaited_once()
        collector.collect_later.assert_called_once()

    async def test_tickets_are_collected_as_ticket_logs(self):
        collector = SimpleNamespace(collect_later=MagicMock())
        cog = self.make_parser(collector)
        message = self.upload(category_id=TICKETS_CATEGORY)

        await cog.on_message(message)

        args, kwargs = collector.collect_later.call_args
        self.assertEqual((args[0], args[1], args[2]), (message, LOG, "latest.log"))
        self.assertEqual(kwargs["channel_kind"], "ticket")

    async def test_admin_approved_logs_are_collected(self):
        collector = SimpleNamespace(collect_later=MagicMock())
        cog = self.make_parser(collector)
        message = self.upload()
        message.attachments[0].filename = "mylog.txt"
        cog._pending[message.id] = [message.attachments[0].url]
        reaction = SimpleNamespace(emoji="🔍", message=message)
        admin = MagicMock(spec=discord.Member)
        admin.bot = False
        admin.guild_permissions = SimpleNamespace(administrator=True)
        admin.guild = SimpleNamespace(id=GUILD)

        with patch.object(log_parser, "is_admin", return_value=True):
            await cog.on_reaction_add(reaction, admin)

        args, kwargs = collector.collect_later.call_args
        self.assertEqual((args[2], kwargs["channel_kind"]), ("mylog.txt", "public"))

    async def test_a_reposted_upload_gets_its_card_credited_to_the_uploader_without_diagnosis(self):
        collector = SimpleNamespace(collect_later=MagicMock())
        cog = self.make_parser(collector)
        repost = self.upload(category_id=TICKETS_CATEGORY)
        repost.author = BOT
        repost.content = ""
        repost.attachments[0].filename = "my-log_2.txt"  # Bulma asked for it: no 🔍 approval for an unusual name
        notes = SimpleNamespace(filename="notes.txt", size=5, url="https://cdn/n", read=AsyncMock(return_value=b"hello"))
        repost.attachments.append(notes)

        await cog.parse_upload(repost, UPLOADER)

        repost.reply.assert_awaited_once()
        cog._add_diagnosis.assert_not_awaited()
        self.assertEqual(cog._pending, {})
        (upload, text, filename, _report), kwargs = collector.collect_later.call_args
        self.assertEqual(
            (upload.id, upload.author, upload.channel, upload.guild, upload.created_at, upload.content),
            (repost.id, UPLOADER, repost.channel, repost.guild, repost.created_at, ""),
        )
        self.assertEqual((text, filename, kwargs["channel_kind"]), (LOG, "my-log_2.txt", "ticket"))


if __name__ == "__main__":
    unittest.main()
