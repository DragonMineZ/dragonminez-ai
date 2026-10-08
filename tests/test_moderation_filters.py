import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# cogs.moderation imports services.mod_actions, whose import chain (mod_cases -> database.db)
# calls load_settings() at import time; these need to exist regardless of run/discovery order.
os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.moderation import HUMAN_ESCALATION_TIMEOUT_SECONDS, ModerationCog, _Incident
from bulmaai.services.moderation import (
    MessageSignal,
    ModerationAction,
    ModerationConfig,
    ModerationDecision,
    ModerationState,
    apply_rule_action,
    evaluate_message,
    exempt_filters,
    parse_filter_rules,
    without_filters,
)


def _signal(**overrides) -> MessageSignal:
    base = dict(guild_id=1, channel_id=2, author_id=3, content="")
    base.update(overrides)
    return MessageSignal(**base)


class BannedWordTests(unittest.TestCase):
    def test_wildcard_matches_obfuscated_join(self) -> None:
        config = ModerationConfig(banned_words=("free*nitro",))
        decision = evaluate_message(
            _signal(content="claim your freenitro code now"), config, ModerationState(), now=1.0
        )
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "banned_word"))

    def test_wildcard_does_not_bridge_across_separate_words(self) -> None:
        config = ModerationConfig(banned_words=("free*nitro",))
        decision = evaluate_message(
            _signal(content="it's free, no strings, go get your nitro"), config, ModerationState(), now=1.0
        )
        self.assertEqual(decision.action, ModerationAction.ALLOW)


class MassMentionTests(unittest.TestCase):
    def test_mentions_at_limit_are_deleted(self) -> None:
        config = ModerationConfig(mass_mention_limit=5)
        decision = evaluate_message(_signal(mention_count=5), config, ModerationState(), now=1.0)
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "mass_mention"))

    def test_mentions_under_limit_are_allowed(self) -> None:
        config = ModerationConfig(mass_mention_limit=5)
        decision = evaluate_message(_signal(mention_count=2), config, ModerationState(), now=1.0)
        self.assertEqual(decision.action, ModerationAction.ALLOW)


class DisabledFilterTests(unittest.TestCase):
    def test_disabled_filter_lets_later_filters_run(self) -> None:
        config = without_filters(ModerationConfig(mass_mention_limit=5, banned_words=("spam",)), ["mass_mention"])
        self.assertEqual(config.banned_words, ("spam",))
        allowed = evaluate_message(_signal(mention_count=9), config, ModerationState(), now=1.0)
        self.assertEqual(allowed.action, ModerationAction.ALLOW)
        caught = evaluate_message(_signal(mention_count=9, content="spam"), config, ModerationState(), now=1.0)
        self.assertEqual(caught.reason, "banned_word")

    def test_disabled_link_burst_keeps_shortener_alert(self) -> None:
        config = without_filters(ModerationConfig(link_burst_count=1), ["link_burst"])
        decision = evaluate_message(_signal(content="https://bit.ly/abc"), config, ModerationState(), now=1.0)
        self.assertEqual(decision.reason, "suspicious_shortener")


class FilterRuleTests(unittest.TestCase):
    RAW = '{"discord_invite": {"action": "alert", "channels": ["10"], "roles": ["20"]}, "link_burst": {"action": "timeout"}, "zalgo": "junk", "x": {"action": "nuke"}}'

    def test_parse_skips_bad_entries_and_unknown_actions(self) -> None:
        rules = parse_filter_rules(self.RAW)
        self.assertEqual(sorted(rules), ["discord_invite", "link_burst", "x"])
        self.assertEqual(rules["x"].action, "")
        self.assertEqual(parse_filter_rules("not json"), {})

    def test_exemption_by_channel_or_role(self) -> None:
        rules = parse_filter_rules(self.RAW)
        self.assertEqual(exempt_filters(rules, {10, None}, set()), ("discord_invite",))
        self.assertEqual(exempt_filters(rules, {99}, {20}), ("discord_invite",))
        self.assertEqual(exempt_filters(rules, {99}, {21}), ())

    def test_action_override_maps_burst_reason_and_skips_purge(self) -> None:
        rules = parse_filter_rules(self.RAW)
        burst = ModerationDecision(action=ModerationAction.TIMEOUT, reason="link burst")
        timed = apply_rule_action(burst, rules, timeout_seconds=600)
        self.assertEqual((timed.action, timed.timeout_seconds, timed.purge, timed.warn), (ModerationAction.TIMEOUT, 600, False, False))
        invite = ModerationDecision(action=ModerationAction.DELETE, reason="discord_invite")
        self.assertEqual(apply_rule_action(invite, rules, timeout_seconds=600).action, ModerationAction.ALERT)
        caps = ModerationDecision(action=ModerationAction.DELETE, reason="excessive_caps")
        self.assertIs(apply_rule_action(caps, rules, timeout_seconds=600), caps)
        warned = apply_rule_action(caps, parse_filter_rules('{"excessive_caps": {"action": "warn"}}'), timeout_seconds=600)
        self.assertEqual((warned.action, warned.warn), (ModerationAction.DELETE, True))


class EveryonePingTests(unittest.TestCase):
    def test_everyone_ping_without_permission_is_deleted(self) -> None:
        config = ModerationConfig(block_everyone_ping=True)
        decision = evaluate_message(
            _signal(content="@everyone check this out", can_mention_everyone=False),
            config,
            ModerationState(),
            now=1.0,
        )
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "everyone_ping"))

    def test_staff_with_permission_is_allowed(self) -> None:
        config = ModerationConfig(block_everyone_ping=True)
        decision = evaluate_message(
            _signal(content="@everyone check this out", can_mention_everyone=True),
            config,
            ModerationState(),
            now=1.0,
        )
        self.assertEqual(decision.action, ModerationAction.ALLOW)


class ExcessiveCapsTests(unittest.TestCase):
    def test_long_all_caps_message_is_deleted(self) -> None:
        config = ModerationConfig(caps_percent=70, caps_min_length=20)
        decision = evaluate_message(
            _signal(content="THIS IS TOTALLY OUT OF CONTROL SPAM"), config, ModerationState(), now=1.0
        )
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "excessive_caps"))

    def test_short_ok_is_not_caps_spam(self) -> None:
        config = ModerationConfig(caps_percent=70, caps_min_length=20)
        decision = evaluate_message(_signal(content="OK"), config, ModerationState(), now=1.0)
        self.assertEqual(decision.action, ModerationAction.ALLOW)


class CodeBlockExemptionTests(unittest.TestCase):
    def test_fenced_log_paste_is_not_caps_or_wall_of_text(self) -> None:
        config = ModerationConfig(caps_percent=70, caps_min_length=20, newline_limit=30)
        log = "\n".join(f"[main/ERROR] FATAL MOD LOADING ERROR LINE {n}" for n in range(40))
        decision = evaluate_message(
            _signal(content=f"my game crashes:\n```\n{log}\n```"), config, ModerationState(), now=1.0
        )
        self.assertEqual(decision.action, ModerationAction.ALLOW)


class ExcessiveEmojiTests(unittest.TestCase):
    def test_over_limit_unicode_emoji_is_deleted(self) -> None:
        config = ModerationConfig(emoji_limit=3)
        decision = evaluate_message(
            _signal(content="\U0001F600\U0001F600\U0001F600\U0001F600"), config, ModerationState(), now=1.0
        )
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "excessive_emoji"))

    def test_normal_message_with_two_emoji_is_allowed(self) -> None:
        config = ModerationConfig(emoji_limit=3)
        decision = evaluate_message(
            _signal(content="nice work \U0001F600\U0001F600"), config, ModerationState(), now=1.0
        )
        self.assertEqual(decision.action, ModerationAction.ALLOW)

    def test_custom_emoji_counts_towards_the_limit(self) -> None:
        config = ModerationConfig(emoji_limit=3)
        decision = evaluate_message(
            _signal(content="<:pog:111><:pog:111><:pog:111>"), config, ModerationState(), now=1.0
        )
        self.assertEqual(decision.reason, "excessive_emoji")


class WallOfTextTests(unittest.TestCase):
    def test_many_newlines_are_deleted(self) -> None:
        config = ModerationConfig(newline_limit=5)
        decision = evaluate_message(
            _signal(content="\n".join(["line"] * 7)), config, ModerationState(), now=1.0
        )
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "wall_of_text"))

    def test_single_code_block_under_the_limit_is_allowed(self) -> None:
        config = ModerationConfig(newline_limit=10)
        content = "```\n" + "\n".join(["some code"] * 3) + "\n```"
        decision = evaluate_message(_signal(content=content), config, ModerationState(), now=1.0)
        self.assertEqual(decision.action, ModerationAction.ALLOW)


class ZalgoTests(unittest.TestCase):
    def test_heavy_combining_marks_are_deleted(self) -> None:
        config = ModerationConfig(zalgo_enabled=True)
        marks = "̵̶̧̖̣́̀̈̐̕"
        decision = evaluate_message(
            _signal(content=f"h{marks}i{marks}"), config, ModerationState(), now=1.0
        )
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "zalgo"))

    def test_normal_accented_text_is_allowed(self) -> None:
        config = ModerationConfig(zalgo_enabled=True)
        decision = evaluate_message(_signal(content="café résumé naïve"), config, ModerationState(), now=1.0)
        self.assertEqual(decision.action, ModerationAction.ALLOW)


class DuplicateSpamTests(unittest.TestCase):
    def test_single_channel_repeats_are_deleted(self) -> None:
        config = ModerationConfig(duplicate_count=3, duplicate_window_seconds=30)
        state = ModerationState()
        content = "buy my discord server now"
        for now in (100.0, 105.0):
            decision = evaluate_message(_signal(content=content, channel_id=2), config, state, now=now)
            self.assertEqual(decision.action, ModerationAction.ALLOW)
        decision = evaluate_message(_signal(content=content, channel_id=2), config, state, now=110.0)
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "duplicate_spam"))

    def test_cross_channel_repeats_are_timed_out(self) -> None:
        config = ModerationConfig(duplicate_count=3, duplicate_window_seconds=30)
        state = ModerationState()
        content = "buy my discord server now"
        evaluate_message(_signal(content=content, channel_id=2), config, state, now=100.0)
        evaluate_message(_signal(content=content, channel_id=7), config, state, now=105.0)
        decision = evaluate_message(_signal(content=content, channel_id=2), config, state, now=110.0)
        self.assertEqual((decision.action, decision.reason), (ModerationAction.TIMEOUT, "duplicate_spam"))


class FastMessagesTests(unittest.TestCase):
    def test_messages_past_the_threshold_are_deleted(self) -> None:
        config = ModerationConfig(fast_message_count=3, fast_message_window_seconds=8)
        state = ModerationState()
        for now in (100.0, 101.0):
            decision = evaluate_message(_signal(), config, state, now=now)
            self.assertEqual(decision.action, ModerationAction.ALLOW)
        decision = evaluate_message(_signal(), config, state, now=102.0)
        self.assertEqual((decision.action, decision.reason), (ModerationAction.DELETE, "fast_messages"))

    def test_messages_outside_the_window_do_not_accumulate(self) -> None:
        config = ModerationConfig(fast_message_count=3, fast_message_window_seconds=8)
        state = ModerationState()
        evaluate_message(_signal(), config, state, now=100.0)
        evaluate_message(_signal(), config, state, now=101.0)
        # Far past the window: the first two events have expired, so this isn't the 3rd hit.
        decision = evaluate_message(_signal(), config, state, now=120.0)
        self.assertEqual(decision.action, ModerationAction.ALLOW)


class EditPathTests(unittest.TestCase):
    def test_fresh_state_per_edit_never_counts_toward_bursts(self) -> None:
        """Mirrors what the cog does for on_message_edit: a fresh ModerationState() every time,
        so an edited message can never look like the Nth hit of a burst/duplicate/fast-message run."""
        config = ModerationConfig(fast_message_count=2, fast_message_window_seconds=8)
        for now in (100.0, 101.0, 102.0, 103.0):
            decision = evaluate_message(_signal(), config, ModerationState(), now=now)
            self.assertEqual(decision.action, ModerationAction.ALLOW)


class EscalationTimeoutTests(unittest.TestCase):
    """Repeated DELETE hits become a TIMEOUT (existing behaviour); the length now depends on shape."""

    @staticmethod
    def _repeat(reason: str) -> ModerationDecision:
        cog = ModerationCog.__new__(ModerationCog)
        cog._incidents = {}
        guild = SimpleNamespace(id=1)
        author = SimpleNamespace(id=3)
        incident = None
        for i in range(3):
            message = SimpleNamespace(id=i, guild=guild, author=author, channel=SimpleNamespace(id=2))
            decision = ModerationDecision(action=ModerationAction.DELETE, reason=reason)
            incident, _ = cog._open_incident(message, decision)
        return incident.decision

    def test_repeated_human_shaped_hits_get_the_short_timeout(self) -> None:
        decision = self._repeat("excessive_caps")
        self.assertEqual(decision.action, ModerationAction.TIMEOUT)
        self.assertEqual(decision.timeout_seconds, HUMAN_ESCALATION_TIMEOUT_SECONDS)

    def test_repeated_spam_shaped_hits_keep_the_default_long_timeout(self) -> None:
        decision = self._repeat("blocked_domain")
        self.assertEqual(decision.action, ModerationAction.TIMEOUT)
        self.assertIsNone(decision.timeout_seconds)


class WarnStrikeTests(unittest.IsolatedAsyncioTestCase):
    async def test_banned_word_hit_warns_once_per_incident(self) -> None:
        cog = ModerationCog.__new__(ModerationCog)
        cog.bot = SimpleNamespace(
            settings=SimpleNamespace(
                moderation_image_burst_timeout_seconds=7 * 24 * 3600,
                moderation_image_burst_purge_seconds=600,
                moderation_log_channel_id=None,
            )
        )
        cog._recent_message_channels = {}
        cog._incidents = {}
        cog._record_case = AsyncMock()

        guild = SimpleNamespace(id=1)
        author = SimpleNamespace(id=3, mention="<@3>")
        channel = SimpleNamespace(id=10, send=AsyncMock())

        def make_message(message_id: int):
            return SimpleNamespace(
                id=message_id,
                guild=guild,
                channel=channel,
                author=author,
                jump_url=f"https://discord.com/channels/1/10/{message_id}",
                attachments=[],
                delete=AsyncMock(),
            )

        decision = ModerationDecision(action=ModerationAction.DELETE, reason="banned_word", details="matched 'x'")

        with (
            patch("bulmaai.services.mod_actions.perform", new=AsyncMock()) as perform,
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=1)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()),
        ):
            await cog._apply_decision(make_message(1), decision)
            await cog._apply_decision(make_message(2), decision)

        perform.assert_awaited_once()
        args, kwargs = perform.call_args
        self.assertEqual(args[0], cog.bot)
        self.assertEqual(args[1], guild)
        self.assertEqual(kwargs["action"], "warn")
        self.assertEqual(kwargs["target_id"], author.id)
        self.assertEqual(kwargs["moderator"], None)
        self.assertEqual(kwargs["source"], "automod")
        self.assertEqual(kwargs["log_case"], False)
        # The channel notice for a DELETE-level human filter also fires at most once per incident.
        self.assertEqual(channel.send.await_count, 1)


class RuleWiringTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, **settings) -> ModerationCog:
        cog = ModerationCog.__new__(ModerationCog)
        values = dict(
            moderation_image_burst_timeout_seconds=7 * 24 * 3600,
            moderation_image_burst_purge_seconds=600,
            moderation_log_channel_id=None,
            moderation_disabled_filters=("zalgo",),
            moderation_filter_rules="",
        )
        values.update(settings)
        cog.bot = SimpleNamespace(settings=SimpleNamespace(**values))
        cog._recent_message_channels = {}
        cog._incidents = {}
        cog._record_case = AsyncMock()
        return cog

    def test_thread_inherits_parent_channel_exemption_and_role_exemption(self) -> None:
        cog = self._cog(moderation_filter_rules='{"discord_invite": {"channels": ["10"]}, "mass_mention": {"roles": ["7"]}}')
        thread = SimpleNamespace(id=55, parent_id=10, category_id=None)
        member = SimpleNamespace(roles=[SimpleNamespace(id=7)])
        self.assertEqual(cog._filters_off(SimpleNamespace(channel=thread, author=member)), {"zalgo", "discord_invite", "mass_mention"})
        self.assertEqual(cog._filters_off(SimpleNamespace(channel=SimpleNamespace(id=11), author=SimpleNamespace(roles=[]))), {"zalgo"})

    async def _warned(self, decision: ModerationDecision) -> bool:
        cog = self._cog()
        message = SimpleNamespace(
            id=1, guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=10, send=AsyncMock()),
            author=SimpleNamespace(id=3, mention="<@3>"), jump_url="https://discord.com/channels/1/10/1",
            attachments=[], delete=AsyncMock(),
        )
        with (
            patch("bulmaai.services.mod_actions.perform", new=AsyncMock()) as perform,
            patch("bulmaai.services.automod_hits.record_hit", AsyncMock(return_value=1)),
            patch("bulmaai.services.automod_hits.update_hit", AsyncMock()),
        ):
            await cog._apply_decision(message, decision)
        return perform.await_count == 1

    async def test_rule_warn_flag_overrides_the_filter_default(self) -> None:
        self.assertFalse(await self._warned(ModerationDecision(action=ModerationAction.DELETE, reason="banned_word", warn=False)))
        self.assertTrue(await self._warned(ModerationDecision(action=ModerationAction.DELETE, reason="excessive_caps", warn=True)))


class HandledCardTests(unittest.IsolatedAsyncioTestCase):
    """cogs/mod_interactions.py collapses the alert card when a mod clicks a button;
    the debounced re-render must then leave the message alone."""

    async def test_flush_log_update_skips_a_handled_card_and_rerenders_an_open_one(self) -> None:
        from discord.components import _component_factory

        from bulmaai.cogs import moderation as moderation_cog
        from bulmaai.ui.mod_cards import collapsed_alert

        self.addCleanup(
            setattr, moderation_cog, "LOG_UPDATE_DEBOUNCE_SECONDS", moderation_cog.LOG_UPDATE_DEBOUNCE_SECONDS
        )
        moderation_cog.LOG_UPDATE_DEBOUNCE_SECONDS = 0

        cog = ModerationCog.__new__(ModerationCog)
        edit_calls: list[dict] = []

        class LogMessage:
            components: list = []

            async def edit(self, **kwargs):
                edit_calls.append(kwargs)

        message = SimpleNamespace(
            id=1,
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=3),
            channel=SimpleNamespace(id=2),
            attachments=[],
        )
        incident = _Incident(
            decision=ModerationDecision(action=ModerationAction.DELETE, reason="excessive_caps"),
            first_message=message,
        )
        incident.log_message = LogMessage()
        incident.revision = 1

        await cog._flush_log_update(incident)  # still open: re-rendered
        self.assertEqual(len(edit_calls), 1)
        self.assertEqual(edit_calls[0]["allowed_mentions"].everyone, False)

        collapsed = collapsed_alert("✅ **Handled** · x", ["Timed out"], [])
        incident.log_message.components = [_component_factory(p) for p in collapsed.to_components()]
        incident.revision = 2
        await cog._flush_log_update(incident)
        self.assertEqual(len(edit_calls), 1)  # handled: untouched


if __name__ == "__main__":
    unittest.main()


class AuditBypassTests(unittest.TestCase):
    def test_invisible_and_fullwidth_letters_still_match_banned_words(self) -> None:
        config = ModerationConfig(banned_words=("slur",))
        for text in ("s\u2060lur", "s\u00adlur", "\uff53\uff4c\uff55\uff52"):
            decision = evaluate_message(_signal(content=text), config, ModerationState(), now=1.0)
            self.assertEqual(decision.reason, "banned_word", ascii(text))

    def test_filenames_never_count_as_a_link_burst_but_real_links_do(self) -> None:
        config = ModerationConfig(link_burst_count=5, link_burst_window_seconds=60)
        state = ModerationState()
        for i, text in enumerate(["latest.log broke", "config.toml?", "net.minecraft.client.Main", "debug.log", "mods.toml", "x.log"]):
            self.assertEqual(evaluate_message(_signal(content=text), config, state, now=float(i)).reason, "allowed", text)
        spam = [evaluate_message(_signal(content=f"https://s{i}.example/x"), config, state, now=10.0 + i) for i in range(5)]
        self.assertEqual(spam[-1].reason, "link burst")

    def test_forwarded_message_text_is_checked_like_the_members_own(self) -> None:
        forwarded = SimpleNamespace(message=SimpleNamespace(content="free nitro https://evil.example", attachments=[]))
        member = MagicMock(spec=discord.Member, id=3)
        member.guild_permissions.mention_everyone = False
        message = SimpleNamespace(
            guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2), author=member, content="",
            snapshots=[forwarded], raw_mentions=[], raw_role_mentions=[], attachments=[],
        )
        signal = ModerationCog._message_signal(message)
        self.assertIn("https://evil.example", signal.content)
