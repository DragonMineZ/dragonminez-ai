import asyncio
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# cogs.moderation imports services.mod_actions, whose import chain (mod_cases -> database.db)
# calls load_settings() at import time; these need to exist regardless of run/discovery order.
os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.moderation import ModerationCog, scam_check_applies
from bulmaai.services.moderation import (
    FILTER_OFF,
    MessageSignal,
    ModerationAction,
    ModerationConfig,
    ModerationDecision,
    ModerationState,
    apply_rule_action,
    evaluate_message,
    parse_filter_rules,
    without_filters,
)

HELPER_ROLE = 14


def _signal(**overrides) -> MessageSignal:
    base = dict(guild_id=1, channel_id=2, author_id=3, content="hey <@200>", pings_staff=True)
    base.update(overrides)
    return MessageSignal(**base)


class VerbalRuleActionTests(unittest.TestCase):
    def test_verbal_keeps_the_filters_own_fate_and_never_warns_or_purges(self) -> None:
        rules = parse_filter_rules(
            '{"banned_word": {"action": "verbal"}, "link_burst": {"action": "verbal"}, '
            '"suspicious_shortener": {"action": "verbal"}}'
        )
        cases = (
            (ModerationAction.DELETE, "banned_word", ModerationAction.DELETE),
            (ModerationAction.TIMEOUT, "link burst", ModerationAction.DELETE),
            (ModerationAction.ALERT, "suspicious_shortener", ModerationAction.ALERT),
        )
        for action, reason, expected in cases:
            result = apply_rule_action(ModerationDecision(action=action, reason=reason), rules, timeout_seconds=600)
            self.assertEqual((result.action, result.verbal, result.warn, result.purge), (expected, True, False, False))

    def test_other_rule_actions_clear_a_default_verbal(self) -> None:
        staff_ping = ModerationDecision(action=ModerationAction.ALERT, reason="staff_ping", verbal=True)
        for action, expected in (
            ("alert", ModerationAction.ALERT),
            ("delete", ModerationAction.DELETE),
            ("warn", ModerationAction.DELETE),
            ("timeout", ModerationAction.TIMEOUT),
        ):
            rules = parse_filter_rules(f'{{"staff_ping": {{"action": "{action}"}}}}')
            result = apply_rule_action(staff_ping, rules, timeout_seconds=600)
            self.assertEqual((result.action, result.verbal), (expected, False), action)
        self.assertIs(apply_rule_action(staff_ping, {}, timeout_seconds=600), staff_ping)


class StaffPingFilterTests(unittest.TestCase):
    def test_staff_ping_is_a_verbal_that_keeps_the_message(self) -> None:
        decision = evaluate_message(_signal(), ModerationConfig(block_staff_ping=True), ModerationState(), now=1.0)
        self.assertEqual((decision.action, decision.reason, decision.verbal), (ModerationAction.ALERT, "staff_ping", True))

    def test_other_filters_win_over_a_staff_ping(self) -> None:
        config = ModerationConfig(block_staff_ping=True, banned_words=("slur",))
        decision = evaluate_message(_signal(content="<@200> slur"), config, ModerationState(), now=1.0)
        self.assertEqual(decision.reason, "banned_word")
        self.assertTrue(scam_check_applies(ModerationDecision(ModerationAction.ALERT, "staff_ping", verbal=True), enforcing=False))

    def test_off_when_disabled_or_no_staff_pinged(self) -> None:
        on = ModerationConfig(block_staff_ping=True)
        off = without_filters(on, ["staff_ping"])
        self.assertEqual(FILTER_OFF["staff_ping"], {"block_staff_ping": False})
        self.assertEqual(evaluate_message(_signal(), off, ModerationState(), now=1.0).action, ModerationAction.ALLOW)
        self.assertEqual(
            evaluate_message(_signal(pings_staff=False), on, ModerationState(), now=1.0).action, ModerationAction.ALLOW
        )


class StaffPingSignalTests(unittest.TestCase):
    settings = SimpleNamespace(
        panel_owner_role_ids=(), panel_admin_role_ids=(), panel_moderator_role_ids=(), panel_helper_role_ids=(HELPER_ROLE,)
    )

    def message(self, author_roles=(), *, users=(200,)):
        staff = SimpleNamespace(id=200, roles=[SimpleNamespace(id=HELPER_ROLE)])
        author = MagicMock(spec=discord.Member, id=3, roles=[SimpleNamespace(id=r) for r in author_roles])
        author.guild_permissions.mention_everyone = False
        return SimpleNamespace(
            guild=SimpleNamespace(id=1, get_member=lambda uid: staff if uid == 200 else None),
            channel=SimpleNamespace(id=2), author=author, content="hey", snapshots=[],
            raw_mentions=list(users), raw_role_mentions=[], attachments=[],
        )

    def test_only_new_messages_from_non_staff_that_ping_staff(self) -> None:
        signal = ModerationCog._message_signal
        self.assertTrue(signal(self.message(), self.settings).pings_staff)
        self.assertFalse(signal(self.message(), self.settings, is_edit=True).pings_staff)
        self.assertFalse(signal(self.message((HELPER_ROLE,)), self.settings).pings_staff)
        self.assertFalse(signal(self.message(users=(555,)), self.settings).pings_staff)
        self.assertFalse(signal(self.message()).pings_staff)


class VerbalWarningCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cases: list[dict] = []  # fake mod_cases rows

        async def record_case(**kwargs):
            self.cases.append({**kwargs, "active": True, "created_at": datetime.now(timezone.utc)})
            return len(self.cases)

        async def count_active_since(guild_id, user_id, action, since):
            return sum(1 for c in self.cases if c["action"] == action and c["active"] and c["created_at"] >= since)

        async def deactivate_cases_since(guild_id, user_id, action, since, *, note=None):
            await asyncio.sleep(0)  # let a racing message reach its own UPDATE
            ended = [c for c in self.cases if c["action"] == action and c["active"] and c["created_at"] >= since]
            for case in ended:
                case["active"] = False
            return len(ended)

        self.perform = AsyncMock()
        self.hits = AsyncMock(return_value=1)
        patches = (
            patch("bulmaai.services.mod_cases.record_case", record_case),
            patch("bulmaai.services.mod_cases.count_active_since", count_active_since),
            patch("bulmaai.services.mod_cases.deactivate_cases_since", deactivate_cases_since),
            patch("bulmaai.services.mod_actions.perform", self.perform),
            patch("bulmaai.services.automod_hits.record_hit", self.hits),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cog = ModerationCog.__new__(ModerationCog)
        self.cog.bot = SimpleNamespace(settings=SimpleNamespace())
        self.cog._incidents = {}
        self.cog._send_log = AsyncMock()

    def message(self, message_id: int):
        author = SimpleNamespace(id=3, mention="<@3>")
        return SimpleNamespace(
            id=message_id,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2, send=AsyncMock()),
            author=author,
            reply=AsyncMock(),
            delete=AsyncMock(),
        )

    def decision(self, action=ModerationAction.ALERT):
        return ModerationDecision(action=action, reason="staff_ping", verbal=True)

    async def test_third_verbal_in_an_hour_is_one_warn_and_ends_the_verbals(self) -> None:
        notices = []
        for i in range(3):
            message = self.message(i)
            self.assertFalse(await self.cog._apply_decision(message, self.decision()))
            message.delete.assert_not_called()  # a staff ping stays up
            notices.append(message.reply.await_args.args[0])
            mentions = message.reply.await_args.kwargs["allowed_mentions"]
            self.assertEqual((mentions.everyone, mentions.roles, mentions.replied_user), (False, False, False))
        self.assertIn("(1/3 this hour)", notices[0])
        self.assertIn("(2/3 this hour)", notices[1])
        self.assertIn("so you've been warned", notices[2])
        self.perform.assert_awaited_once()
        kwargs = self.perform.await_args.kwargs
        self.assertEqual((kwargs["action"], kwargs["source"], kwargs["target_id"]), ("warn", "automod", 3))
        self.assertIn("verbal warnings in an hour (pinging staff)", kwargs["reason"])
        self.assertTrue(all(c["action"] == "verbal" and c["source"] == "automod" for c in self.cases))
        self.assertFalse(any(c["active"] for c in self.cases))
        self.assertEqual(self.cog._incidents, {})  # no alert card
        self.cog._send_log.assert_not_called()
        self.assertEqual(self.hits.await_args.kwargs["action"], "verbal")

        await self.cog._apply_decision(self.message(9), self.decision())
        self.perform.assert_awaited_once()  # the next warn needs 3 fresh verbals

    async def test_racing_messages_warn_only_once(self) -> None:
        for i in range(2):
            await self.cog._apply_decision(self.message(i), self.decision())
        await asyncio.gather(*(self.cog._apply_decision(self.message(10 + i), self.decision()) for i in range(2)))
        self.assertEqual(self.perform.await_count, 1)

    async def test_deleting_verbal_notice_goes_to_the_channel(self) -> None:
        message = self.message(1)
        await self.cog._apply_decision(message, self.decision(ModerationAction.DELETE))
        message.delete.assert_awaited_once()
        message.reply.assert_not_called()
        self.assertEqual(message.channel.send.await_args.kwargs["delete_after"], 6)

    async def test_database_failure_still_sends_the_notice(self) -> None:
        message = self.message(1)
        with patch("bulmaai.services.mod_cases.record_case", AsyncMock(side_effect=RuntimeError("db down"))):
            with self.assertLogs("bulmaai.cogs.moderation", "ERROR"):
                await self.cog._apply_decision(message, self.decision())
        self.assertIn("This is a verbal warning.", message.reply.await_args.args[0])
        self.perform.assert_not_called()
