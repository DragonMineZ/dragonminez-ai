import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.mod_log_sync import map_audit_entry, parse_dyno_case, parse_duration_seconds
from bulmaai.services import mod_cases

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BOT_ID = 1
DYNO_ID = 999
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

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
        )

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
            mapped = map_audit_entry(
                entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
            )
            self.assertEqual(mapped.action, expected)

    def test_bot_executor_is_skipped(self):
        entry = make_entry(discord.AuditLogAction.ban, user_id=BOT_ID)

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
        )

        self.assertIsNone(mapped)

    def test_dyno_executor_source_when_modlog_not_configured(self):
        entry = make_entry(discord.AuditLogAction.ban, user_id=DYNO_ID)

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
        )

        self.assertEqual(mapped.source, "dyno")
        self.assertEqual(mapped.moderator_id, DYNO_ID)

    def test_dyno_executor_skipped_when_modlog_configured(self):
        entry = make_entry(discord.AuditLogAction.ban, user_id=DYNO_ID)

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=True
        )

        self.assertIsNone(mapped)

    def test_member_update_timeout_set_computes_duration(self):
        after = SimpleNamespace(communication_disabled_until=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc))
        entry = make_entry(discord.AuditLogAction.member_update, after=after, created_at=NOW)

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
        )

        self.assertEqual(mapped.action, "timeout")
        self.assertEqual(mapped.duration_seconds, 3600)

    def test_member_update_timeout_cleared_is_untimeout(self):
        after = SimpleNamespace(communication_disabled_until=None)
        entry = make_entry(discord.AuditLogAction.member_update, after=after)

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
        )

        self.assertEqual(mapped.action, "untimeout")
        self.assertIsNone(mapped.duration_seconds)

    def test_member_update_unrelated_change_is_skipped(self):
        after = SimpleNamespace(nick="new nickname")  # no communication_disabled_until at all
        entry = make_entry(discord.AuditLogAction.member_update, after=after)

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
        )

        self.assertIsNone(mapped)

    def test_member_prune_has_no_single_target_and_is_skipped(self):
        entry = make_entry(discord.AuditLogAction.member_prune, target_id=None)
        entry.target = None

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
        )

        self.assertIsNone(mapped)

    def test_missing_target_id_is_skipped(self):
        entry = make_entry(discord.AuditLogAction.ban, target_id=None)
        entry.target = None

        mapped = map_audit_entry(
            entry, bot_user_id=BOT_ID, dyno_user_id=DYNO_ID, dyno_modlog_configured=False
        )

        self.assertIsNone(mapped)


class ParseDynoCaseTests(unittest.TestCase):
    def test_mention_style_ban_embed(self):
        embed = {
            "title": "Case #12 | Ban",
            "fields": [
                {"name": "User", "value": "<@123456789012345678>"},
                {"name": "Moderator", "value": "<@998877665544332211>"},
                {"name": "Reason", "value": "raiding"},
            ],
        }

        parsed = parse_dyno_case(embed)

        self.assertEqual(parsed.case_number, 12)
        self.assertEqual(parsed.action, "ban")
        self.assertEqual(parsed.user_id, 123456789012345678)
        self.assertEqual(parsed.moderator_id, 998877665544332211)
        self.assertEqual(parsed.reason, "raiding")
        self.assertIsNone(parsed.duration_seconds)

    def test_name_id_style_kick_with_no_reason(self):
        embed = {
            "author": {"name": "Case 45 | Kick | SomeUser#1234"},
            "fields": [
                {"name": "User", "value": "SomeUser#1234 (222222222222222222)"},
                {"name": "Moderator", "value": "Mod#0001 (333333333333333333)"},
                {"name": "Reason", "value": "No reason given"},
            ],
        }

        parsed = parse_dyno_case(embed)

        self.assertEqual(parsed.case_number, 45)
        self.assertEqual(parsed.action, "kick")
        self.assertEqual(parsed.user_id, 222222222222222222)
        self.assertEqual(parsed.moderator_id, 333333333333333333)
        self.assertIsNone(parsed.reason)

    def test_mute_normalizes_to_timeout_with_length(self):
        embed = {
            "title": "Case 7 | Mute | SomeUser",
            "fields": [
                {"name": "User", "value": "<@444444444444444444>"},
                {"name": "Moderator", "value": "<@555555555555555555>"},
                {"name": "Length", "value": "1 day, 2 hours"},
            ],
        }

        parsed = parse_dyno_case(embed)

        self.assertEqual(parsed.action, "timeout")
        self.assertEqual(parsed.duration_seconds, 86400 + 2 * 3600)

    def test_unmute_normalizes_to_untimeout(self):
        embed = {
            "title": "Case 8 | Unmute | SomeUser",
            "fields": [{"name": "User", "value": "<@444444444444444444>"}],
        }

        parsed = parse_dyno_case(embed)

        self.assertEqual(parsed.action, "untimeout")

    def test_falls_back_to_footer_id_when_no_user_field(self):
        embed = {
            "title": "Case 9 | Warn",
            "footer": {"text": "ID: 666666666666666666"},
            "fields": [{"name": "Reason", "value": "spam"}],
        }

        parsed = parse_dyno_case(embed)

        self.assertEqual(parsed.action, "warn")
        self.assertEqual(parsed.user_id, 666666666666666666)

    def test_unparseable_embed_returns_none(self):
        embed = {"title": "Just an announcement", "fields": []}

        self.assertIsNone(parse_dyno_case(embed))

    def test_embed_without_user_id_anywhere_returns_none(self):
        embed = {"title": "Case 1 | Ban", "fields": [{"name": "Reason", "value": "x"}]}

        self.assertIsNone(parse_dyno_case(embed))

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
        self.assertEqual(args[-3], "audit:123")
        self.assertEqual(args[-2], NOW)
        self.assertIsNone(args[-1])  # expires_at

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


if __name__ == "__main__":
    unittest.main()
