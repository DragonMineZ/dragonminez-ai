import hashlib
import hmac
import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs.build_gate import push_choice_label
from bulmaai.services import build_gate

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

    def test_choice_label_marks_latest_and_fits_discord_limit(self):
        label = push_choice_label(rank=0, branch="b" * 60, sha="abcdef123", title="t" * 80, author="Goku")
        self.assertTrue(label.startswith("★ Latest"))
        self.assertLessEqual(len(label), 100)
        self.assertNotIn("Latest", push_choice_label(rank=1, branch="b", sha="abcdef123", title="t", author="Goku"))


if __name__ == "__main__":
    unittest.main()
