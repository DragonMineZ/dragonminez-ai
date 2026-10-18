import os
import types
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs import log_parser
from bulmaai.cogs.log_parser import AI_FOOTER, AI_HEADER, LogParserCog, _build_card
from bulmaai.services import ai_budget
from bulmaai.utils.log_parser import parse_log
from tests.v2_helpers import texts

GUILD_ID = 100
TICKETS_CATEGORY = 200
STAFF_ROLE = 1
PATRON_ROLE = 2
TESTER_ROLE = 3

_CRASH_LOG = (
    "[12:00:00] [main/INFO]: ModLauncher running: args [--fml.forgeVersion, 47.4.10, --fml.mcVersion, 1.20.1]\n"
    "[12:00:05] [Render thread/ERROR]: Failed to load dragonminez config\n"
    "java.lang.IllegalStateException: broken\n"
    "\tat com.example.Foo.bar(Foo.java:10)\n"
)
_CLEAN_LOG = "[12:00:00] [main/INFO]: ModLauncher running: args [--fml.forgeVersion, 47.4.10, --fml.mcVersion, 1.20.1]\n"


def _settings(**overrides):
    values = dict(
        log_ai_enabled=True,
        log_ai_daily_limit=3,
        log_ai_patron_daily_limit=10,
        dev_jar_patreon_role_ids=(PATRON_ROLE,),
        dev_jar_tester_role_ids=(TESTER_ROLE,),
        discord_staff_role_ids=(STAFF_ROLE,),
        panel_guild_id=GUILD_ID,
        dev_guild_id=None,
        ai_ticket_category_id=TICKETS_CATEGORY,
        ai_daily_small_token_limit=None,
        ai_daily_big_token_limit=None,
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _message(*, roles=(), category_id=None, guild_id=GUILD_ID, content=""):
    author = types.SimpleNamespace(
        id=7, name="goku", display_name="Goku", roles=[types.SimpleNamespace(id=r) for r in roles]
    )
    return types.SimpleNamespace(
        guild=types.SimpleNamespace(id=guild_id),
        channel=types.SimpleNamespace(id=5, name="help", category_id=category_id),
        author=author,
        content=content,
    )


def _result(reply="Update GeckoLib to 1.0.4.", **extra):
    return {"reply": reply, "kind": "answer", "guard_flags": frozenset(), "paused": False, **extra}


class CardTests(unittest.IsolatedAsyncioTestCase):  # views need a running loop
    async def test_diagnosis_goes_right_after_what_went_wrong(self) -> None:
        report = parse_log(_CRASH_LOG)
        blocks = texts(_build_card(report, "latest.log", diagnosis="Update GeckoLib.\n-# 📖 [Crashes](<https://wiki.dragonminez.com/Crashes>)"))
        self.assertTrue(blocks[1].startswith("**❌ Errors / Fatal"))
        self.assertEqual(
            blocks[2],
            f"{AI_HEADER}\nUpdate GeckoLib.\n-# 📖 [Crashes](<https://wiki.dragonminez.com/Crashes>)\n{AI_FOOTER}",
        )
        self.assertTrue(blocks[3].startswith("**🖥️ Environment**"))

    async def test_long_diagnosis_is_trimmed_but_keeps_header_and_footer(self) -> None:
        report = parse_log(_CRASH_LOG)
        blocks = texts(_build_card(report, "latest.log", diagnosis="x" * 6000))
        self.assertLessEqual(sum(map(len, blocks)), 4000)
        self.assertTrue(blocks[2].startswith(AI_HEADER) and blocks[2].endswith(AI_FOOTER))

    async def test_limit_note_replaces_the_diagnosis(self) -> None:
        report = parse_log(_CRASH_LOG)
        blocks = texts(_build_card(report, "latest.log", limit_note="-# 🤖 limit"))
        self.assertEqual(blocks[2], "-# 🤖 limit")
        self.assertNotIn(AI_HEADER, "\n".join(blocks))

    async def test_plain_card_is_unchanged(self) -> None:
        report = parse_log(_CRASH_LOG)
        self.assertEqual(texts(_build_card(report, "latest.log")), texts(_build_card(report, "latest.log", diagnosis=None)))
        self.assertNotIn(AI_HEADER, "\n".join(texts(_build_card(report, "latest.log"))))


class DiagnosisFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ai_budget.reset()
        self.cog = LogParserCog.__new__(LogParserCog)  # skips __init__, which starts the purge loop
        self.cog.bot = types.SimpleNamespace(settings=_settings())
        self.sent = types.SimpleNamespace(id=1, edit=AsyncMock())

    async def _diagnose(self, message=None, *, log=_CRASH_LOG, result=None, remaining=3, approved=False, error=None):
        agent = AsyncMock(return_value=result if result is not None else _result(), side_effect=error)
        with (
            patch.object(log_parser, "run_support_agent", agent),
            patch.object(log_parser.log_ai_quota, "remaining", AsyncMock(return_value=remaining)) as left,
            patch.object(log_parser.log_ai_quota, "record", AsyncMock()) as record,
        ):
            await self.cog._add_diagnosis(
                self.sent, message or _message(), parse_log(log), "latest.log", None, approved=approved
            )
        return agent, left, record

    def _edited_text(self) -> str:
        self.sent.edit.assert_awaited_once()
        return "\n".join(texts(self.sent.edit.await_args.kwargs["view"]))

    async def test_member_gets_a_diagnosis_and_it_counts(self) -> None:
        agent, left, record = await self._diagnose()

        left.assert_awaited_once_with(7, 3)
        record.assert_awaited_once_with(7)
        kwargs = agent.await_args.kwargs
        self.assertEqual((kwargs["enabled_tools"], kwargs["workflow_prefix"]), ([], "log_diagnosis"))
        self.assertEqual((kwargs["language_hint"], kwargs["channel_kind"], kwargs["ticket_conversation"]), ("en", "public", False))
        content = kwargs["messages"][0]["content"]
        self.assertIn("latest.log", content)
        self.assertIn("Errors (1):", content)
        self.assertNotIn("com.example.Foo.bar", content)  # the summary, never the raw log
        self.assertIn("Never ask questions back", "\n".join(kwargs["context_lines"]))
        self.assertIn(f"{AI_HEADER}\nUpdate GeckoLib to 1.0.4.\n{AI_FOOTER}", self._edited_text())

    async def test_over_the_limit_shows_the_limit_line_instead(self) -> None:
        agent, _, record = await self._diagnose(remaining=0)

        agent.assert_not_awaited()
        record.assert_not_awaited()
        self.assertIn("-# 🤖 Daily AI diagnosis limit reached (3/day; patrons get 10).", self._edited_text())

    async def test_patrons_and_testers_get_the_patron_limit(self) -> None:
        for role in (PATRON_ROLE, TESTER_ROLE):
            self.sent.edit.reset_mock()
            _, left, _ = await self._diagnose(_message(roles=[role]), remaining=0)
            left.assert_awaited_once_with(7, 10)
            text = self._edited_text()
            self.assertIn("-# 🤖 Daily AI diagnosis limit reached (10/day).", text)
            self.assertNotIn("patrons get", text)

    async def test_staff_are_unlimited_and_not_recorded(self) -> None:
        agent, left, record = await self._diagnose(_message(roles=[STAFF_ROLE]), remaining=0)

        left.assert_not_awaited()
        record.assert_not_awaited()
        self.assertTrue(agent.await_args.kwargs["requester_is_staff"])
        self.assertIn(AI_HEADER, self._edited_text())

    async def test_admin_approved_parse_always_runs_and_is_not_counted(self) -> None:
        agent, left, record = await self._diagnose(remaining=0, approved=True)

        left.assert_not_awaited()
        record.assert_not_awaited()
        agent.assert_awaited_once()
        self.assertIn(AI_HEADER, self._edited_text())

    async def test_skipped_in_tickets_outside_the_main_guild_without_errors_or_when_off(self) -> None:
        cases = [
            dict(message=_message(category_id=TICKETS_CATEGORY)),
            dict(message=_message(guild_id=999)),
            dict(log=_CLEAN_LOG),
        ]
        for case in cases:
            agent, left, _ = await self._diagnose(**case)
            agent.assert_not_awaited()
            left.assert_not_awaited()
        self.cog.bot.settings.log_ai_enabled = False
        agent, _, _ = await self._diagnose()
        agent.assert_not_awaited()
        self.sent.edit.assert_not_awaited()

    async def test_paused_budget_skips_before_counting(self) -> None:
        self.cog.bot.settings.ai_daily_small_token_limit = 0
        agent, left, _ = await self._diagnose()
        agent.assert_not_awaited()
        left.assert_not_awaited()
        self.sent.edit.assert_not_awaited()

    async def test_ai_failures_leave_the_card_as_posted(self) -> None:
        for kwargs in (
            dict(error=RuntimeError("boom")),
            dict(result=_result(reply="(no reply)")),
            dict(result=_result(paused=True)),
            dict(result=_result(reply="(no reply)", guard_flags=frozenset({"banned_word"}))),
            dict(result=_result(guard_flags=frozenset({"unknown_link"}))),
            dict(result=_result(kind="handoff")),
        ):
            _, _, record = await self._diagnose(**kwargs)
            self.sent.edit.assert_not_awaited()
            if "error" in kwargs or kwargs["result"].get("paused"):
                record.assert_not_awaited()

    async def test_language_comes_from_the_uploader_message(self) -> None:
        agent, _, _ = await self._diagnose(_message(content="hola, el juego se cierra cuando entro al mundo"))
        self.assertEqual(agent.await_args.kwargs["language_hint"], "es")
        agent, _, _ = await self._diagnose(_message(content="ayuda"))
        self.assertEqual(agent.await_args.kwargs["language_hint"], "en")


if __name__ == "__main__":
    unittest.main()
