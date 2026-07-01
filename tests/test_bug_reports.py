import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.config import load_settings
from bulmaai.services.bug_report_ai import DuplicateAssessment, _coerce_triage
from bulmaai.ui.bug_report_views import apply_status, build_triage_embed


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
        self.assertEqual(settings.bug_report_forum_channel_id, 1484275827146363061)
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

    def test_coerce_uses_fallback_title_when_missing(self) -> None:
        triage = _coerce_triage({}, fallback_title="My Forum Post")
        self.assertFalse(triage.is_bug)
        self.assertEqual(triage.title, "My Forum Post")
        self.assertEqual(triage.severity, "medium")


class BugTriageEmbedTests(unittest.TestCase):
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

    def test_embed_has_no_github_reference(self) -> None:
        embed = build_triage_embed(self._sample_triage(), status="triaged", reporter_id=42)
        blob = (embed.title or "") + (embed.description or "")
        for field in embed.fields:
            blob += f"{field.name}{field.value}"
        self.assertNotIn("github", blob.lower())
        self.assertIn("<@42>", blob)

    def test_apply_status_updates_status_field(self) -> None:
        embed = build_triage_embed(self._sample_triage(), status="triaged", reporter_id=42)
        updated = apply_status(embed, "resolved")
        status_values = [field.value for field in updated.fields if field.name == "Status"]
        self.assertEqual(len(status_values), 1)
        self.assertIn("Resolved", status_values[0])

    def test_apply_status_supports_duplicate_and_fixed_display_labels(self) -> None:
        embed = build_triage_embed(self._sample_triage(), status="triaged", reporter_id=42)

        dup = apply_status(embed, "duplicate")
        dup_status = next(f.value for f in dup.fields if f.name == "Status")
        self.assertIn("duplicate", dup_status.lower())

        fixed = apply_status(embed, "fixed")
        fixed_status = next(f.value for f in fixed.fields if f.name == "Status")
        self.assertIn("fixed", fixed_status.lower())

    def test_embed_renders_duplicate_suggestion(self) -> None:
        duplicate = DuplicateAssessment(
            match_type="duplicate",
            issue_number=123,
            issue_title="Crash when transforming",
            confidence="high",
            reason="Same crash on the transform menu.",
        )
        embed = build_triage_embed(self._sample_triage(), reporter_id=42, duplicate=duplicate)
        dup_fields = [f for f in embed.fields if "duplicate" in f.name.lower()]
        self.assertEqual(len(dup_fields), 1)
        self.assertIn("#123", dup_fields[0].value)

    def test_embed_renders_already_fixed_suggestion(self) -> None:
        duplicate = DuplicateAssessment(
            match_type="already_fixed",
            issue_number=88,
            issue_title="Transform crash",
            confidence="medium",
            reason="Fixed in a merged PR.",
        )
        embed = build_triage_embed(self._sample_triage(), reporter_id=42, duplicate=duplicate)
        fixed_fields = [f for f in embed.fields if "already fixed" in f.name.lower()]
        self.assertEqual(len(fixed_fields), 1)
        self.assertIn("#88", fixed_fields[0].value)

    def test_embed_omits_suggestion_when_no_match(self) -> None:
        no_match = DuplicateAssessment("none", None, "", "low", "")
        self.assertFalse(no_match.has_match)
        embed = build_triage_embed(self._sample_triage(), reporter_id=42, duplicate=no_match)
        names = " ".join(f.name.lower() for f in embed.fields)
        self.assertNotIn("duplicate", names)
        self.assertNotIn("already fixed", names)


if __name__ == "__main__":
    unittest.main()
