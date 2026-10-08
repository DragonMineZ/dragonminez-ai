import hashlib
import hmac
import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bulmaai.services import build_gate
from bulmaai.ui.build_gate_views import (
    CHANGELOG_PREFIX,
    changelog_button,
    gate_buttons,
    gate_card,
    preview_card,
)

REPO = "DragonMineZ/dragonminez"


def push(*, ref="refs/heads/feature", files=("src/Foo.java",), **overrides):
    payload = {
        "ref": ref,
        "after": "a" * 40,
        "repository": {"full_name": REPO},
        "pusher": {"name": "goku"},
        "commits": [
            {"id": "a" * 40, "message": "Fix ki\n\nlonger body", "author": {"name": "Goku"}, "url": "u", "modified": list(files)}
        ],
    }
    payload.update(overrides)
    return payload


class ParsePushTests(unittest.TestCase):
    def test_java_change_prompts(self):
        info = build_gate.parse_push(push(), repo_full_name=REPO)
        self.assertEqual((info.branch, info.pusher), ("feature", "goku"))
        self.assertEqual(info.commits[0]["title"], "Fix ki")
        self.assertEqual(info.commits[0]["description"], "longer body")

    def test_non_java_main_tags_deletes_and_other_repos_skip(self):
        self.assertIsNone(build_gate.parse_push(push(files=("README.md", "x.json")), repo_full_name=REPO))
        self.assertIsNone(build_gate.parse_push(push(ref="refs/heads/main"), repo_full_name=REPO))
        self.assertIsNone(build_gate.parse_push(push(ref="refs/tags/v1"), repo_full_name=REPO))
        self.assertIsNone(build_gate.parse_push(push(deleted=True), repo_full_name=REPO))
        self.assertIsNone(build_gate.parse_push(push(), repo_full_name="Other/repo"))

    def test_removed_and_added_java_count(self):
        payload = push()
        payload["commits"][0] = {**payload["commits"][0], "modified": [], "removed": ["Old.java"]}
        self.assertIsNotNone(build_gate.parse_push(payload, repo_full_name=REPO))

    def test_truncated_push_assumes_java(self):
        commits = [{"id": str(i), "message": "m", "author": {"name": "a"}, "modified": ["a.md"]} for i in range(20)]
        self.assertIsNotNone(build_gate.parse_push(push(commits=commits, size=25), repo_full_name=REPO))
        self.assertIsNone(build_gate.parse_push(push(commits=commits, size=20), repo_full_name=REPO))


class HelperTests(unittest.TestCase):
    def test_signature(self):
        body = b'{"a":1}'
        good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(build_gate.verify_signature("s3cret", body, good))
        self.assertFalse(build_gate.verify_signature("s3cret", body, "sha256=bad"))
        self.assertFalse(build_gate.verify_signature("s3cret", body, None))
        self.assertFalse(build_gate.verify_signature(None, body, good))

    def test_merge_commits_dedupes_and_keeps_order(self):
        a, b, c = ({"sha": s} for s in "abc")
        self.assertEqual(build_gate.merge_commits((a, b), (b, c)), (a, b, c))

    def test_commits_input_drops_descriptions_when_too_big(self):
        big = tuple({"sha": str(i), "title": "t", "author": "a", "url": "u", "description": "x" * 5000} for i in range(30))
        text = build_gate.commits_input(big)
        self.assertLessEqual(len(text), build_gate.MAX_INPUT_CHARS)
        self.assertNotIn("description", text)

    def test_progress(self):
        job = {"steps": [
            {"name": "Set up job", "status": "completed", "conclusion": "success"},
            {"name": "Checkout", "status": "completed", "conclusion": "success"},
            {"name": "Build", "status": "in_progress"},
            {"name": "Upload", "status": "queued"},
            {"name": "Post Checkout", "status": "queued"},
        ]}
        steps = build_gate.visible_steps(job)
        self.assertEqual([s["name"] for s in steps], ["Checkout", "Build", "Upload"])
        self.assertEqual([build_gate.step_icon(s) for s in steps], ["✅", "🔄", "⬜"])
        self.assertTrue(build_gate.progress_bar(1, 3).endswith("33%"))
        self.assertEqual(build_gate.step_icon({"status": "completed", "conclusion": "failure"}), "❌")


if __name__ == "__main__":
    unittest.main()


def build_request(**overrides):
    fields = dict(
        id=7, repo=REPO, branch="feature", head_sha="a" * 40, pusher="goku", commits=(), source="push",
        status=build_gate.PENDING, channel_id=1, message_id=2, expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        decided_by=None, decided_at=None, run_id=None, run_url=None, created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        changelog=None, preview_message_id=None,
    )
    fields.update(overrides)
    return build_gate.BuildRequest(**fields)


class ChangelogTextTests(unittest.TestCase):
    def test_blank_becomes_none_and_long_text_is_capped(self):
        self.assertIsNone(build_gate.clean_changelog(None))
        self.assertIsNone(build_gate.clean_changelog("  \n "))
        self.assertEqual(build_gate.clean_changelog("  hi  "), "hi")
        self.assertEqual(len(build_gate.clean_changelog("x" * 9000)), build_gate.MAX_CHANGELOG_CHARS)


def _walk(item):
    yield item
    for child in getattr(item, "items", None) or getattr(item, "children", None) or []:
        yield from _walk(child)


def card_text(view) -> str:
    return "\n".join(item.content for item in _walk(view) if isinstance(item, discord.ui.TextDisplay))


def card_buttons(view) -> list:
    return [item for item in _walk(view) if isinstance(item, discord.ui.Button)]


class ChangelogViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_has_changelog_button_that_switches_label_once_set(self):
        labels = [child.label for child in gate_buttons(build_request()).children]
        self.assertEqual(labels, ["Build jar", "Add changelog", "Skip"])
        self.assertEqual(changelog_button(build_request(changelog="x")).label, "Edit changelog")
        self.assertEqual(changelog_button(build_request()).custom_id, f"{CHANGELOG_PREFIX}7")
        self.assertEqual(len(card_buttons(preview_card(build_request()))), 1)
        self.assertEqual(card_buttons(preview_card(build_request(), locked=True)), [])

    async def test_gate_card_shows_changelog_only_when_set(self):
        self.assertNotIn("**Changelog**", card_text(gate_card(build_request(), recent=[], window_minutes=10)))
        text = card_text(gate_card(build_request(changelog="x" * 4000), recent=[], window_minutes=10))
        self.assertIn("**Changelog**", text)
        self.assertLessEqual(len(text.split("**Changelog**\n", 1)[1]), 1000)

    async def test_preview_matches_public_whats_new(self):
        self.assertIn("### ✨ What's New\nShiny form", card_text(preview_card(build_request(changelog="Shiny form"))))
        self.assertIn("No changelog", card_text(preview_card(build_request())))
        self.assertIn("Final", card_text(preview_card(build_request(), locked=True)))


class SaveChangelogTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        import asyncio
        from bulmaai.cogs.build_gate import BuildGateCog

        cog = BuildGateCog.__new__(BuildGateCog)
        cog._changelog_lock = asyncio.Lock()
        cog.repo_full_name = REPO
        cog.settings = SimpleNamespace(build_gate_window_minutes=10)
        cog._edit_message = AsyncMock()
        cog._edit_preview = AsyncMock()
        return cog

    def make_interaction(self):
        return SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(mention="<@1>"),
        )

    async def test_pending_request_refreshes_prompt(self):
        cog, interaction = self.make_cog(), self.make_interaction()
        request = build_request(changelog="hi")
        with patch.object(build_gate, "set_changelog", AsyncMock(return_value=request)), patch.object(
            build_gate, "recent", AsyncMock(return_value=[])
        ):
            await cog._save_changelog(interaction, 7, "hi")
        cog._edit_message.assert_awaited_once()
        cog._edit_preview.assert_not_awaited()
        self.assertIn("saved", interaction.followup.send.await_args.args[0].lower())

    async def test_building_request_refreshes_preview(self):
        cog, interaction = self.make_cog(), self.make_interaction()
        request = build_request(status=build_gate.BUILDING, changelog="hi")
        with patch.object(build_gate, "set_changelog", AsyncMock(return_value=request)):
            await cog._save_changelog(interaction, 7, "hi")
        cog._edit_preview.assert_awaited_once_with(request, locked=False)
        cog._edit_message.assert_not_awaited()

    async def test_finished_build_rejects_the_edit(self):
        cog, interaction = self.make_cog(), self.make_interaction()
        with patch.object(build_gate, "set_changelog", AsyncMock(return_value=None)):
            await cog._save_changelog(interaction, 7, "late")
        cog._edit_preview.assert_not_awaited()
        cog._edit_message.assert_not_awaited()
        self.assertIn("locked", interaction.followup.send.await_args.args[0])
