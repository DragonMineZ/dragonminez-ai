import asyncio
import json
import os
import unittest

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bulmaai.services import ai_budget, ai_tools, code_notes
from bulmaai.services.ai_client import get_schemas

REPO = "DragonMineZ/dragonminez"
SETTINGS = SimpleNamespace(
    code_notes_enabled=True,
    code_notes_model="claude-haiku-5-5",
    mod_source_branch="main",
    ai_support_timeout_seconds=30,
    ai_daily_small_token_limit=10**9,
)
DAY = datetime(2026, 11, 1, tzinfo=timezone.utc)


def raw_commit(sha="a" * 40, message="Fix ki blast", **extra):
    return {
        "id": sha,
        "message": message,
        "timestamp": "2026-11-01T10:00:00+00:00",
        "author": {"name": "Goku", "username": "goku"},
        **extra,
    }


def push(*commits, ref="refs/heads/main", **extra):
    return {"ref": ref, "repository": {"full_name": REPO}, "commits": list(commits), **extra}


def commit(sha="a" * 40):
    return code_notes.PushCommit(sha, "main", "Fix ki blast", "goku", "Goku", None)


def response(payload):
    return SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))], usage=None
    )


def make_note(**overrides):
    values = dict(
        sha="abcdef1234", repo=REPO, branch="main", author_login="goku", author_name="Goku", committed_at=DAY,
        message="m", note="Did x.", files=[{"path": "A.java"}], created_at=DAY,
    )
    values.update(overrides)
    return code_notes.CodeNote(**values)


class PushCommitsTests(unittest.TestCase):
    def test_main_branch_commit_is_kept_with_author(self):
        (item,) = code_notes.push_commits(push(raw_commit()), repo_full_name=REPO)
        self.assertEqual((item.branch, item.author_login, item.author_name), ("main", "goku", "Goku"))
        self.assertEqual(item.committed_at, datetime(2026, 11, 1, 10, tzinfo=timezone.utc))

    def test_other_repo_tag_and_deleted_branch_are_skipped(self):
        self.assertEqual(code_notes.push_commits(push(raw_commit()), repo_full_name="Other/repo"), [])
        self.assertEqual(code_notes.push_commits(push(raw_commit(), ref="refs/tags/v1"), repo_full_name=REPO), [])
        self.assertEqual(code_notes.push_commits(push(raw_commit(), deleted=True), repo_full_name=REPO), [])

    def test_a_sha_listed_twice_merges_and_already_seen_commits_are_skipped(self):
        payload = push(
            raw_commit("a" * 40),
            raw_commit("a" * 40),
            raw_commit("b" * 40, distinct=False),
            raw_commit("c" * 40, message="Merge branch 'x' into main"),
            raw_commit("d" * 40),
        )
        self.assertEqual([c.sha for c in code_notes.push_commits(payload, repo_full_name=REPO)], ["a" * 40, "d" * 40])


class DiffTests(unittest.TestCase):
    def test_only_source_files_are_kept(self):
        files = [
            {"filename": "src/main/java/A.java", "status": "modified", "additions": 1, "deletions": 0, "patch": "+x"},
            {"filename": "src/main/resources/assets/dragonminez/lang/en_us.json", "status": "modified", "patch": "+y"},
            {"filename": "build.gradle", "status": "modified", "patch": "+z"},
            {"filename": "mods.toml", "status": "added", "patch": "+w"},
            {"filename": "src/main/resources/tex/a.png", "status": "added"},
            {"filename": "README.md", "status": "modified", "patch": "+r"},
            {"filename": "src/generated/resources/data/x.json", "status": "added", "patch": "+g"},
        ]
        text, kept = code_notes.build_diff(files)
        self.assertEqual(
            [item["path"] for item in kept],
            ["src/main/java/A.java", "src/main/resources/assets/dragonminez/lang/en_us.json", "build.gradle", "mods.toml"],
        )
        self.assertIn("### src/main/java/A.java (modified, +1 -0)\n+x", text)
        self.assertNotIn("README", text)
        self.assertNotIn("generated", text)

    def test_no_source_files_means_nothing_kept(self):
        self.assertEqual(code_notes.build_diff([{"filename": "README.md", "patch": "x"}]), ("", []))

    def test_long_patches_and_the_whole_diff_are_capped_with_a_note(self):
        big = "+" + "x" * (code_notes.FILE_DIFF_CHARS * 2)
        files = [{"filename": f"A{i}.java", "status": "modified", "patch": big} for i in range(10)]
        text, kept = code_notes.build_diff(files, limit=20000)
        self.assertEqual(len(kept), 10)
        self.assertLessEqual(len(text), 20000 + 100)
        self.assertIn("truncated", text)

    def test_default_cap_is_forty_thousand(self):
        files = [{"filename": f"A{i}.java", "patch": "+" + "x" * 8000} for i in range(10)]
        text, _ = code_notes.build_diff(files)
        self.assertLessEqual(len(text), code_notes.DIFF_CHARS + 100)
        self.assertIn("[diff truncated]", text)


class ParseNoteTests(unittest.TestCase):
    def test_note_lines(self):
        note = code_notes.parse_note(
            {"what": "Ki blasts cost less.", "why": "Balance.", "areas": ["ki", "combat"], "config_keys": ["kiCost"]}
        )
        self.assertEqual(note, "Ki blasts cost less. Why: Balance.\nAreas: ki, combat\nConfig/commands: kiCost")

    def test_unstated_why_and_empty_lists_are_left_out(self):
        note = code_notes.parse_note({"what": "Renamed a class.", "why": "Not stated.", "areas": [], "config_keys": []})
        self.assertEqual(note, "Renamed a class.")

    def test_bad_payloads_give_none(self):
        for bad in (None, [], "x", {}, {"what": "  "}, {"areas": ["x"]}):
            self.assertIsNone(code_notes.parse_note(bad))

    def test_junk_lists_are_ignored_and_long_text_cut(self):
        note = code_notes.parse_note({"what": "w" * 1000, "why": "", "areas": "ki", "config_keys": [None, "", "key"]})
        self.assertEqual(len(note.split("\n")[0]), code_notes.FIELD_CHARS)
        self.assertTrue(note.endswith("Config/commands: key"))


class ProcessCommitsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        code_notes._inflight.clear()
        code_notes.clear_context_cache()
        ai_budget.reset()

    def github(self, files=None):
        github = MagicMock()
        github.get_commit = AsyncMock(
            return_value={"files": files or [{"filename": "A.java", "status": "modified", "patch": "+x"}]}
        )
        github.get_file = AsyncMock(return_value=("# Context\nKi is energy.", "sha"))
        return github

    def client(self, payload=None):
        client = MagicMock()
        client.messages.create = AsyncMock(
            return_value=response(payload or {"what": "Changed A.", "why": "Not stated.", "areas": ["x"], "config_keys": []})
        )
        return client

    async def test_a_new_commit_is_summarized_and_stored(self):
        github, client = self.github(), self.client()
        with patch.object(code_notes, "exists", AsyncMock(return_value=False)), patch.object(
            code_notes, "store", AsyncMock(return_value=True)
        ) as store:
            stored = await code_notes.process_commits(SETTINGS, github, [commit()], repo=REPO, client=client)
        self.assertEqual(stored, 1)
        args = store.await_args.args
        self.assertEqual(args[2], "Changed A.\nAreas: x")
        self.assertEqual(args[3][0]["path"], "A.java")
        kwargs = client.messages.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertIn("Ki is energy.", str(kwargs["system"]))
        self.assertIn("untrusted data", kwargs["system"][0]["text"])
        self.assertIn("+x", kwargs["messages"][0]["content"])

    async def test_a_commit_already_stored_is_not_summarized_again(self):
        github, client = self.github(), self.client()
        with patch.object(code_notes, "exists", AsyncMock(return_value=True)), patch.object(
            code_notes, "store", AsyncMock()
        ) as store:
            stored = await code_notes.process_commits(SETTINGS, github, [commit()], repo=REPO, client=client)
        self.assertEqual(stored, 0)
        github.get_commit.assert_not_awaited()
        client.messages.create.assert_not_awaited()
        store.assert_not_awaited()

    async def test_the_same_commit_pushed_on_two_branches_is_written_once(self):
        github, client = self.github(), self.client()
        seen: set[str] = set()

        async def exists(sha):
            return sha in seen

        async def store(c, repo, note, files):
            seen.add(c.sha)
            return True

        with patch.object(code_notes, "exists", exists), patch.object(code_notes, "store", store):
            results = await asyncio.gather(
                code_notes.process_commits(SETTINGS, github, [commit()], repo=REPO, client=client),
                code_notes.process_commits(SETTINGS, github, [commit()], repo=REPO, client=client),
            )
            await code_notes.process_commits(SETTINGS, github, [commit()], repo=REPO, client=client)
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(client.messages.create.await_count, 1)

    async def test_docs_only_commit_costs_no_model_call(self):
        github = self.github(files=[{"filename": "README.md", "patch": "+x"}])
        client = self.client()
        with patch.object(code_notes, "exists", AsyncMock(return_value=False)), patch.object(
            code_notes, "store", AsyncMock()
        ) as store:
            await code_notes.process_commits(SETTINGS, github, [commit()], repo=REPO, client=client)
        client.messages.create.assert_not_awaited()
        store.assert_not_awaited()

    async def test_one_failure_does_not_stop_the_next_commit(self):
        github, client = self.github(), self.client()
        github.get_commit.side_effect = [RuntimeError("boom"), {"files": [{"filename": "A.java", "patch": "+x"}]}]
        with patch.object(code_notes, "exists", AsyncMock(return_value=False)), patch.object(
            code_notes, "store", AsyncMock(return_value=True)
        ):
            stored = await code_notes.process_commits(
                SETTINGS, github, [commit("a" * 40), commit("b" * 40)], repo=REPO, client=client
            )
        self.assertEqual(stored, 1)

    async def test_missing_context_file_is_skipped(self):
        github, client = self.github(), self.client()
        github.get_file.side_effect = RuntimeError("404")
        with patch.object(code_notes, "exists", AsyncMock(return_value=False)), patch.object(
            code_notes, "store", AsyncMock(return_value=True)
        ):
            await code_notes.process_commits(SETTINGS, github, [commit()], repo=REPO, client=client)
        self.assertEqual(len(client.messages.create.await_args.kwargs["system"]), 1)

    async def test_paused_ai_stores_nothing(self):
        github, client = self.github(), self.client()
        with patch.object(ai_budget, "is_paused", return_value=True), patch.object(
            code_notes, "exists", AsyncMock(return_value=False)
        ):
            stored = await code_notes.process_commits(SETTINGS, github, [commit()], repo=REPO, client=client)
        self.assertEqual(stored, 0)
        client.messages.create.assert_not_awaited()


class StaffToolTests(unittest.TestCase):
    def test_the_tool_is_only_in_the_staff_draft_set(self):
        name = ai_tools.CODE_NOTES_TOOL
        self.assertIn(name, ai_tools.TOOL_SCHEMAS)
        self.assertIn(name, ai_tools.STAFF_DRAFT_TOOL_NAMES)
        for names in (ai_tools.SUPPORT_TOOL_NAMES, ai_tools.LOOKUP_TOOL_NAMES):
            self.assertNotIn(name, names)
            self.assertNotIn(name, [schema["name"] for schema in get_schemas(names)])
        self.assertIn(name, [schema["name"] for schema in get_schemas(ai_tools.STAFF_DRAFT_TOOL_NAMES)])

    def test_only_the_staff_draft_call_site_asks_for_it(self):
        from pathlib import Path

        hits = [
            path.name
            for path in Path(__file__).resolve().parent.parent.joinpath("src", "bulmaai").rglob("*.py")
            if "STAFF_DRAFT_TOOL_NAMES" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(sorted(hits), ["ai_tickets.py", "ai_tools.py"])


class StaffToolRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_returns_short_notes_without_author(self):
        with patch.object(code_notes, "search", AsyncMock(return_value=[make_note()])):
            out = await ai_tools.search_code_notes("ki")
        self.assertEqual(out["notes"][0]["commit"], "abcdef1")
        self.assertEqual(out["notes"][0]["files"], ["A.java"])
        self.assertNotIn("goku", str(out))

    async def test_a_staff_tool_is_refused_when_not_enabled(self):
        from bulmaai.services import ai_client

        tool_use = SimpleNamespace(type="tool_use", id="t1", name=ai_tools.CODE_NOTES_TOOL, input={"query": "ki"})
        first = SimpleNamespace(stop_reason="tool_use", content=[tool_use], usage=None, id="r1")
        second = SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ok")], usage=None, id="r2"
        )
        search = AsyncMock(return_value=[])
        with patch.object(ai_client, "_create_message", AsyncMock(side_effect=[first, second])), patch.object(
            code_notes, "search", search
        ):
            result, _trace = await ai_client._run_once(
                model="claude-haiku-5-5",
                effort="low",
                use_tools=True,
                workflow="support_question",
                context_text="",
                conversation=[{"role": "user", "content": "hi"}],
                data_text="",
                knowledge_hits=[],
                enabled_tools=ai_tools.LOOKUP_TOOL_NAMES,
                language="en",
                settings=SimpleNamespace(
                    ai_support_max_output_tokens=1000, ai_support_timeout_seconds=5, ai_support_escalation_confidence=0.7
                ),
                user_id=1,
                channel_id=2,
                ticket_conversation=False,
                bot=None,
                requester_is_staff=False,
                channel_kind="public",
            )
        search.assert_not_awaited()
        self.assertEqual(result["tool_results"][0]["output"], {"error": "tool_failed"})


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, **settings):
        from bulmaai.cogs.build_gate import BuildGateCog

        cog = BuildGateCog.__new__(BuildGateCog)
        cog.repo_full_name = REPO
        cog.github = MagicMock()
        cog.settings = SimpleNamespace(code_notes_enabled=True, ai_daily_small_token_limit=10**9, **settings)
        return cog

    async def test_a_main_branch_push_schedules_the_job(self):
        ai_budget.reset()
        cog = self.make_cog()
        done = asyncio.Event()
        seen = []

        async def fake(settings, github, commits, *, repo):
            seen.append((commits, repo))
            done.set()
            return len(commits)

        with patch.object(code_notes, "process_commits", fake):
            cog._queue_code_notes(push(raw_commit()), asyncio.get_running_loop())
            await asyncio.wait_for(done.wait(), 2)
        self.assertEqual(seen[0][1], REPO)
        self.assertEqual(seen[0][0][0].branch, "main")

    async def test_disabled_paused_or_empty_pushes_schedule_nothing(self):
        ai_budget.reset()
        process = AsyncMock()
        with patch.object(code_notes, "process_commits", process):
            off = self.make_cog()
            off.settings.code_notes_enabled = False
            off._queue_code_notes(push(raw_commit()), asyncio.get_running_loop())
            with patch.object(ai_budget, "is_paused", return_value=True):
                self.make_cog()._queue_code_notes(push(raw_commit()), asyncio.get_running_loop())
            self.make_cog()._queue_code_notes(push(), asyncio.get_running_loop())
            await asyncio.sleep(0.05)
        process.assert_not_called()

    async def test_a_failure_scheduling_never_raises(self):
        cog = self.make_cog()
        with patch.object(code_notes, "push_commits", side_effect=RuntimeError("boom")):
            cog._queue_code_notes(push(raw_commit()), asyncio.get_running_loop())

    def test_build_gate_still_ignores_a_main_push(self):
        from bulmaai.services import build_gate

        payload = push(raw_commit(modified=["A.java"]), after="a" * 40)
        self.assertIsNone(build_gate.parse_push(payload, repo_full_name=REPO, ignore_branches=("main",)))
        self.assertEqual(len(code_notes.push_commits(payload, repo_full_name=REPO)), 1)


class DevlogCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_lists_short_sha_author_date_and_one_line(self):
        from bulmaai.cogs.devlog import devlog_card
        from tests.v2_helpers import text

        note = make_note(note="Did x @everyone.\nAreas: ki")
        shown = text(devlog_card([note]))
        self.assertIn("`abcdef1`", shown)
        self.assertIn("**goku**", shown)
        self.assertIn("Did x", shown)
        self.assertNotIn("Areas: ki", shown)
        self.assertNotIn("@everyone", shown)
        self.assertIn("No code notes", text(devlog_card([], "zzz")))


if __name__ == "__main__":
    unittest.main()
