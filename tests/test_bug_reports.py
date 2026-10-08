import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from discord.components import _component_factory


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.config import load_settings
from bulmaai.services.bug_report_ai import DuplicateAssessment, _coerce_triage
from bulmaai.ui import bug_report_views as views


class BugReportConfigTests(unittest.TestCase):
    def test_bug_report_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "dummy-discord-token",
                "OPENAI_KEY": "dummy-openai-key",
                "GH_APP_PRIVATE_KEY_PEM": "dummy-github-key",
            },
            clear=True,
        ):
            settings = load_settings(include_overrides=False)

        self.assertTrue(settings.bug_reports_enabled)
        self.assertEqual(settings.bug_report_forum_channel_id, 1526421736336265377)
        self.assertEqual(settings.bug_report_repo, "dragonminez")
        self.assertEqual(settings.bug_report_poll_minutes, 10)
        self.assertEqual(settings.openai_bugreport_model, "gpt-5-mini")

    def test_bug_report_settings_are_environment_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BUG_REPORTS_ENABLED": "false",
                "BUG_REPORT_FORUM_CHANNEL_ID": "999",
                "BUG_REPORT_REPO": "dragonminez_ai",
                "BUG_REPORT_POLL_MINUTES": "3",
                "OPENAI_BUGREPORT_MODEL": "gpt-4.1-mini",
            },
            clear=False,
        ):
            settings = load_settings(include_overrides=False)

        self.assertFalse(settings.bug_reports_enabled)
        self.assertEqual(settings.bug_report_forum_channel_id, 999)
        self.assertEqual(settings.bug_report_repo, "dragonminez_ai")
        self.assertEqual(settings.bug_report_poll_minutes, 3)
        self.assertEqual(settings.openai_bugreport_model, "gpt-4.1-mini")


class BugTriageCoercionTests(unittest.TestCase):
    def test_coerce_normalizes_invalid_severity_and_steps(self) -> None:
        triage = _coerce_triage(
            {
                "is_bug": True,
                "title": "Crash on transform",
                "summary": "Game crashes",
                "severity": "apocalyptic",
                "affected_area": "Transformations",
                "steps": ["Open menu", "  ", "Transform", 5],
            },
            fallback_title="fallback",
        )
        self.assertTrue(triage.is_bug)
        self.assertEqual(triage.severity, "medium")
        self.assertEqual(triage.steps, ["Open menu", "Transform", "5"])

    def test_coerce_strips_links_and_pings_the_bot_would_repost(self) -> None:
        triage = _coerce_triage(
            {
                "title": "Crash @everyone",
                "summary": "Staff-confirmed fix: [download hotfix](https://evil.tld/fix.jar)",
                "steps": ["Get it at https://evil.tld/x"],
            },
            fallback_title="fallback",
        )
        text = " ".join([triage.title, triage.summary, *triage.steps])
        self.assertNotIn("evil.tld", text)
        self.assertNotRegex(text, r"@(everyone|here)")
        self.assertIn("download hotfix", triage.summary)

    def test_coerce_uses_fallback_title_when_missing(self) -> None:
        triage = _coerce_triage({}, fallback_title="My Forum Post")
        self.assertFalse(triage.is_bug)
        self.assertEqual(triage.title, "My Forum Post")
        self.assertEqual(triage.severity, "medium")


class BugTriageCardTests(unittest.IsolatedAsyncioTestCase):
    def _sample_triage(self):
        return _coerce_triage(
            {
                "is_bug": True,
                "title": "Crash on transform",
                "summary": "Game crashes when transforming.",
                "severity": "high",
                "affected_area": "Transformations",
                "steps": ["Open the form menu", "Select Super Saiyan"],
            },
            fallback_title="fallback",
        )

    @staticmethod
    def _posted(view):
        """The card as Discord would hand it back on a message."""
        return SimpleNamespace(
            flags=SimpleNamespace(is_components_v2=True),
            components=[_component_factory(c) for c in view.to_components()],
        )

    def _card(self, **kwargs):
        return self._posted(views.triage_view(self._sample_triage(), thread_id=9, reporter_id=42, **kwargs))

    async def test_card_has_no_github_reference_and_names_the_reporter(self) -> None:
        text = views.message_text(self._card())
        self.assertNotIn("github", text.lower())
        self.assertIn("<@42>", text)

    async def test_card_text_round_trips_the_triage_details(self) -> None:
        text = views.message_text(self._card())
        self.assertIn("**Severity** High", text)
        self.assertIn("**Area** Transformations", text)
        self.assertIn("**Steps to reproduce**\n1. Open the form menu\n2. Select Super Saiyan", text)

    async def test_restatus_swaps_the_status_line_keeps_the_reporter_and_drops_buttons(self) -> None:
        for status, label in (("resolved", "Resolved"), ("duplicate", "duplicate"), ("fixed", "fixed")):
            view = views.restatus(self._card(), status)["view"]
            text = views.message_text(self._posted(view))
            self.assertIn(label, text.split("**Status**")[1])
            self.assertIn("<@42>", text)
            self.assertIsNone(view.get_item(views.ACTIONS_ID))

    async def test_card_renders_duplicate_and_fixed_suggestions(self) -> None:
        duplicate = DuplicateAssessment("duplicate", 123, "Crash when transforming", "high", "Same crash.")
        self.assertIn(f"**{views.DUPLICATE_TITLE}** #123", views.message_text(self._card(duplicate=duplicate)))
        fixed = DuplicateAssessment("already_fixed", 88, "Transform crash", "medium", "Fixed.")
        self.assertIn(f"**{views.FIXED_TITLE}** Closed issue #88", views.message_text(self._card(duplicate=fixed)))
        no_match = DuplicateAssessment("none", None, "", "low", "")
        text = views.message_text(self._card(duplicate=no_match))
        self.assertNotIn(views.DUPLICATE_TITLE, text)
        self.assertNotIn(views.FIXED_TITLE, text)


if __name__ == "__main__":
    unittest.main()
