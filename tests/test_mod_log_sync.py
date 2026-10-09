import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.mod_log_sync import ModLogSyncCog, map_audit_entry
from bulmaai.services.mod_actions import parse_duration_seconds
from bulmaai.services import mod_cases

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BOT_ID = 1
OTHER_BOT_ID = 999
MOD_ID = 222
TARGET_ID = 333


def make_entry(
    action,
    *,
    user_id=MOD_ID,
    target_id=TARGET_ID,
    reason=None,
    entry_id=1,
    created_at=NOW,
    after=None,
):
    return SimpleNamespace(
        action=action,
        user=SimpleNamespace(id=user_id) if user_id is not None else None,
        _target_id=target_id,
        target=SimpleNamespace(id=target_id) if target_id is not None else None,
        reason=reason,
        id=entry_id,
        created_at=created_at,
        after=after,
    )


class MapAuditEntryTests(unittest.TestCase):
    def test_ban_by_staff_is_recorded_as_discord_source(self):
        entry = make_entry(discord.AuditLogAction.ban, reason="raiding")

        mapped = map_audit_entry(entry, bot_user_id=BOT_ID)

        self.assertEqual(mapped.action, "ban")
        self.assertEqual(mapped.user_id, TARGET_ID)
        self.assertEqual(mapped.moderator_id, MOD_ID)
        self.assertEqual(mapped.reason, "raiding")
        self.assertEqual(mapped.source, "discord")
        self.assertEqual(mapped.external_id, "audit:1")

    def test_kick_and_unban_map_directly(self):
        for action, expected in (
            (discord.AuditLogAction.kick, "kick"),
            (discord.AuditLogAction.unban, "unban"),
        ):
            entry = make_entry(action)
            mapped = map_audit_entry(entry, bot_user_id=BOT_ID)
            self.assertEqual(mapped.action, expected)

    def test_bot_executor_is_skipped(self):
        entry = make_entry(discord.AuditLogAction.ban, user_id=BOT_ID)

        mapped = map_audit_entry(entry, bot_user_id=BOT_ID)

        self.assertIsNone(mapped)

    def test_other_bot_executor_is_recorded_as_discord_source(self):
        entry = make_entry(discord.AuditLogAction.ban, user_id=OTHER_BOT_ID)

        mapped = map_audit_entry(entry, bot_user_id=BOT_ID)

        self.assertEqual(mapped.source, "discord")
        self.assertEqual(mapped.moderator_id, OTHER_BOT_ID)

    def test_member_update_timeout_set_computes_duration(self):
        after = SimpleNamespace(communication_disabled_until=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc))
        entry = make_entry(discord.AuditLogAction.member_update, after=after, created_at=NOW)

        mapped = map_audit_entry(entry, bot_user_id=BOT_ID)

        self.assertEqual(mapped.action, "timeout")
        self.assertEqual(mapped.duration_seconds, 3600)

    def test_member_update_timeout_cleared_is_untimeout(self):
        after = SimpleNamespace(communication_disabled_until=None)
        entry = make_entry(discord.AuditLogAction.member_update, after=after)

        mapped = map_audit_entry(entry, bot_user_id=BOT_ID)

        self.assertEqual(mapped.action, "untimeout")
        self.assertIsNone(mapped.duration_seconds)

    def test_member_update_unrelated_change_is_skipped(self):
        after = SimpleNamespace(nick="new nickname")  # no communication_disabled_until at all
        entry = make_entry(discord.AuditLogAction.member_update, after=after)

        mapped = map_audit_entry(entry, bot_user_id=BOT_ID)

        self.assertIsNone(mapped)

    def test_member_prune_has_no_single_target_and_is_skipped(self):
        entry = make_entry(discord.AuditLogAction.member_prune, target_id=None)
        entry.target = None

        mapped = map_audit_entry(entry, bot_user_id=BOT_ID)

        self.assertIsNone(mapped)

    def test_missing_target_id_is_skipped(self):
        entry = make_entry(discord.AuditLogAction.ban, target_id=None)
        entry.target = None

        mapped = map_audit_entry(entry, bot_user_id=BOT_ID)

        self.assertIsNone(mapped)


class ParseDurationTests(unittest.TestCase):
    def test_duration_parsing_variants(self):
        self.assertEqual(parse_duration_seconds("30m"), 1800)
        self.assertEqual(parse_duration_seconds("2 days"), 172800)
        self.assertEqual(parse_duration_seconds("1h"), 3600)
        self.assertIsNone(parse_duration_seconds("permanent"))
        self.assertIsNone(parse_duration_seconds(None))


class RecordCaseExternalIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_inserts_with_external_id_and_created_at(self):
        fetchval = AsyncMock(return_value=7)
        pool = SimpleNamespace(fetchval=fetchval)
        with patch("bulmaai.services.mod_cases.get_pool", AsyncMock(return_value=pool)):
            case_id = await mod_cases.record_case(
                guild_id=1,
                user_id=TARGET_ID,
                action="ban",
                moderator_id=MOD_ID,
                reason="raiding",
                source="discord",
                external_id="audit:123",
                created_at=NOW,
            )

        self.assertEqual(case_id, 7)
        args = fetchval.await_args.args
        self.assertIn("ON CONFLICT (external_id)", args[0])
        self.assertEqual(args[-4], "audit:123")
        self.assertEqual(args[-3], NOW)
        self.assertIsNone(args[-2])  # expires_at
        self.assertIsNone(args[-1])  # triggered_by

    async def test_conflicting_external_id_returns_none(self):
        fetchval = AsyncMock(return_value=None)
        pool = SimpleNamespace(fetchval=fetchval)
        with patch("bulmaai.services.mod_cases.get_pool", AsyncMock(return_value=pool)):
            case_id = await mod_cases.record_case(
                guild_id=1,
                user_id=TARGET_ID,
                action="ban",
                source="dyno",
                external_id="dyno:1:12",
            )

        self.assertIsNone(case_id)


class LiveSyncEndsCasesTests(unittest.IsolatedAsyncioTestCase):
    async def sync(self, action, *, user_id=MOD_ID):
        settings = SimpleNamespace(panel_guild_id=1)
        bot = SimpleNamespace(settings=settings, user=SimpleNamespace(id=BOT_ID))
        entry = make_entry(action, user_id=user_id)
        entry.guild = SimpleNamespace(id=1)
        with (
            patch("bulmaai.services.mod_cases.record_case", AsyncMock(return_value=8)),
            patch("bulmaai.services.mod_actions.end_user_cases", AsyncMock()) as end,
            patch("bulmaai.services.mod_actions.post_case_log", AsyncMock()) as post,
        ):
            await ModLogSyncCog(bot).on_audit_log_entry(entry)
        return bot, end, post

    async def test_unban_ends_the_ban_cards_credited_to_the_audit_moderator(self):
        bot, end, post = await self.sync(discord.AuditLogAction.unban)
        end.assert_awaited_once_with(bot, 1, TARGET_ID, "ban", ended_by=MOD_ID)
        post.assert_awaited_once()

    async def test_a_ban_ends_nothing(self):
        _, end, post = await self.sync(discord.AuditLogAction.ban)
        end.assert_not_awaited()
        post.assert_awaited_once()

    async def test_another_bots_ban_gets_a_case_card(self):
        _, _, post = await self.sync(discord.AuditLogAction.ban, user_id=OTHER_BOT_ID)
        post.assert_awaited_once()
        self.assertEqual(post.await_args.kwargs["moderator_id"], OTHER_BOT_ID)
        self.assertEqual(post.await_args.kwargs["source"], "discord")


if __name__ == "__main__":
    unittest.main()
