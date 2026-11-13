import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services import ai_usage, eval_cases, evals, knowledge

import test_feedback_fix as fix_tests  # modules only: importing their TestCase classes would run them twice here
import test_tickets as ticket_tests


def case(**overrides):
    values = dict(
        id=1,
        source="ticket",
        source_ref="ticket:900",
        question="How do I stop ki griefing?",
        reference="Set kiGriefing to false in the server config.",
        language="en",
        category="server",
        expected_docs=("wiki-config.md",),
        exclude_docs=(),
        active=True,
    )
    values.update(overrides)
    return eval_cases.EvalCase(**values)


class RetrievalMetricTests(unittest.TestCase):
    def test_rank_hits_mrr_and_recall(self):
        row = evals.retrieval_row(case(expected_docs=("b", "z")), ["a", "b", "c", "d", "e"], 5)
        self.assertEqual(row["rank"], 2)
        self.assertEqual((row["hit@1"], row["hit@3"], row["hit@k"], row["pass"]), (False, True, True, True))
        self.assertEqual(row["rr"], 0.5)
        self.assertEqual(row["recall"], 0.5)

    def test_a_miss(self):
        row = evals.retrieval_row(case(expected_docs=("z",)), ["a", "b"], 5)
        self.assertIsNone(row["rank"])
        self.assertEqual((row["hit@k"], row["rr"], row["recall"], row["pass"]), (False, 0.0, 0.0, False))

    def test_excluded_docs_are_dropped_before_the_cut(self):
        # Searched with k + len(exclude): the ticket's own doc ranks first but must not count, and the cut stays k.
        row = evals.retrieval_row(
            case(expected_docs=("c",), exclude_docs=("own",)), ["own", "a", "c", "d"], 2
        )
        self.assertEqual(row["got"], ["a", "c"])
        self.assertEqual(row["rank"], 2)
        row = evals.retrieval_row(case(expected_docs=("own",), exclude_docs=("own",)), ["own", "a"], 2)
        self.assertFalse(row["pass"])

    def test_aggregates_overall_and_by_group(self):
        rows = [
            evals.retrieval_row(case(id=1, expected_docs=("a",)), ["a"], 3),
            evals.retrieval_row(case(id=2, expected_docs=("b",), language="es", source="feedback"), ["x", "y", "b"], 3),
            evals.retrieval_row(case(id=3, expected_docs=("c",)), ["x"], 3),
        ]
        metrics = evals.retrieval_metrics(rows, gaps=4)
        overall = metrics["overall"]
        self.assertEqual((overall["n"], overall["gaps"]), (3, 4))
        self.assertAlmostEqual(overall["hit@1"], 1 / 3)
        self.assertAlmostEqual(overall["hit@3"], 2 / 3)
        self.assertAlmostEqual(overall["mrr"], (1 + 1 / 3) / 3)
        self.assertEqual(metrics["by_language"]["es"]["n"], 1)
        self.assertEqual(metrics["by_source"]["ticket"]["n"], 2)
        self.assertEqual(evals.retrieval_metrics([])["overall"], {"n": 0, "gaps": 0})


class AnswerMetricTests(unittest.TestCase):
    def test_score_and_rates_skip_errors(self):
        rows = [
            {"id": 1, "source": "ticket", "language": "en", **evals.parse_grade(
                {"correct": 2, "grounded": True, "handoff": "ok", "language_ok": True, "reason": ""})},
            {"id": 2, "source": "ticket", "language": "es", **evals.parse_grade(
                {"correct": 1, "grounded": False, "handoff": "unneeded", "language_ok": False, "reason": ""})},
            {"id": 3, "source": "ticket", "language": "en", "error": "boom"},
        ]
        overall = evals.answer_metrics(rows)["overall"]
        self.assertEqual((overall["n"], overall["errors"]), (2, 1))
        self.assertEqual(overall["score"], 0.75)
        self.assertEqual((overall["grounded"], overall["handoff_unneeded"], overall["pass"]), (0.5, 0.5, 0.5))


class JudgeParsingTests(unittest.TestCase):
    def test_grade_is_normalized(self):
        grade = evals.parse_grade({"correct": "2", "grounded": "true", "handoff": "OK", "language_ok": True, "reason": " r "})
        self.assertEqual(grade, {"correct": 2, "grounded": True, "handoff": "ok", "language_ok": True, "reason": "r", "pass": True})
        self.assertFalse(evals.parse_grade({"correct": 2, "grounded": False, "handoff": "ok", "language_ok": True})["pass"])

    def test_malformed_grades_raise(self):
        for payload in (
            None,
            [],
            {"correct": 3, "grounded": True, "handoff": "ok", "language_ok": True},
            {"correct": "x", "grounded": True, "handoff": "ok", "language_ok": True},
            {"correct": 1, "grounded": "maybe", "handoff": "ok", "language_ok": True},
            {"correct": 1, "grounded": True, "handoff": "later", "language_ok": True},
        ):
            with self.assertRaises(ValueError, msg=payload):
                evals.parse_grade(payload)

    def test_labels_keep_only_pool_docs_in_pool_order(self):
        self.assertEqual(evals.parse_labels({"docs": ["c", "a", "nope", 5]}, ["a", "b", "c"]), ["a", "c"])
        self.assertEqual(evals.parse_labels({"docs": []}, ["a"]), [])
        for payload in (None, {"docs": "a"}, {}):
            with self.assertRaises(ValueError):
                evals.parse_labels(payload, ["a"])

    def test_label_schema_limits_the_choice_to_the_pool(self):
        schema = evals.label_schema(["a.md", "b.md"])
        self.assertEqual(schema["properties"]["docs"]["items"]["enum"], ["a.md", "b.md"])


class JudgeCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_usage.reset()

    def tearDown(self):
        ai_usage.reset()

    def client(self, payload):
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            model="claude-haiku-5-5",
            usage=SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )
        return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))

    async def test_label_case_pools_both_searches_without_excluded_docs(self):
        hits = {
            "q": [{"filename": "own.md", "title": "", "text": "x"}, {"filename": "a.md", "title": "A", "text": "x"}],
            "r": [{"filename": "a.md", "title": "A", "text": "x"}, {"filename": "b.md", "title": "B", "text": "x"}],
        }
        search = AsyncMock(side_effect=lambda query, limit: hits[query])
        client = self.client({"docs": ["b.md"]})
        docs = await evals.label_case(case(question="q", reference="r", exclude_docs=("own.md",)), search=search, client=client)
        self.assertEqual(docs, ["b.md"])
        self.assertEqual([call.args for call in search.await_args_list], [("q", 11), ("r", 11)])
        kwargs = client.messages.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertEqual(kwargs["output_config"]["effort"], "high")
        self.assertEqual(kwargs["output_config"]["format"]["schema"]["properties"]["docs"]["items"]["enum"], ["a.md", "b.md"])
        self.assertNotIn("own.md", kwargs["messages"][0]["content"])
        self.assertEqual(ai_usage.pending()[0][1], evals.JUDGE_FEATURE)

    async def test_empty_pool_needs_no_call(self):
        client = self.client({"docs": []})
        self.assertEqual(await evals.label_case(case(), search=AsyncMock(return_value=[]), client=client), [])
        client.messages.create.assert_not_called()

    async def test_answer_case_hides_excluded_docs_and_grades(self):
        seen = {}

        async def agent(**kwargs):
            seen.update(kwargs)
            seen["excluded"] = knowledge.EXCLUDED_DOCS.get()
            return {"reply": "Set kiGriefing to false.", "kind": "answer", "model": "claude-haiku-5-5"}

        client = self.client({"correct": 2, "grounded": True, "handoff": "ok", "language_ok": True, "reason": "fine"})
        row = await evals.answer_case(
            case(exclude_docs=("own.md",)), agent=agent, client=client, tools=["t"], settings=object(), model="m"
        )
        self.assertEqual(seen["excluded"], frozenset({"own.md"}))
        self.assertEqual(knowledge.EXCLUDED_DOCS.get(), frozenset())  # reset after the case
        self.assertEqual(
            {key: seen[key] for key in ("user_id", "channel_id", "ticket_conversation", "channel_kind", "workflow_prefix", "bot", "model_override", "language_hint")},
            {"user_id": 0, "channel_id": 0, "ticket_conversation": True, "channel_kind": "ticket", "workflow_prefix": "eval", "bot": None, "model_override": "m", "language_hint": "en"},
        )
        self.assertEqual(seen["messages"][0]["speaker_id"], "0")
        self.assertEqual((row["correct"], row["pass"], row["reply"]), (2, True, "Set kiGriefing to false."))

    async def test_a_right_handoff_passes_and_a_wrong_one_does_not(self):
        async def agent(**kwargs):
            return {"reply": "Staff will take a look.", "kind": "handoff"}

        for verdict, expected in (("ok", True), ("unneeded", False)):
            client = self.client({"correct": 0, "grounded": True, "handoff": verdict, "language_ok": True, "reason": ""})
            row = await evals.answer_case(case(), agent=agent, client=client, tools=[], settings=object())
            self.assertEqual(row["pass"], expected, verdict)

    async def test_a_failing_case_becomes_an_error_row(self):
        async def worker(item):
            if item.id == 2:
                raise RuntimeError("down")
            return {"id": item.id}

        rows = await evals.gather_limited([case(id=1), case(id=2, language="es")], worker)
        self.assertEqual(rows[0], {"id": 1})
        self.assertEqual((rows[1]["id"], rows[1]["language"], rows[1]["error"]), (2, "es", "RuntimeError: down"))


class KnowledgeExclusionTests(unittest.IsolatedAsyncioTestCase):
    def pool(self, filenames):
        rows = [{"filename": name, "title": name, "url": None, "text": "t"} for name in filenames]
        return SimpleNamespace(fetch=AsyncMock(return_value=rows))

    async def test_default_is_unchanged(self):
        pool = self.pool(["a", "b"])
        hits = await knowledge.search("ki griefing", 2, pool=pool)
        self.assertEqual([hit["filename"] for hit in hits], ["a", "b"])
        self.assertEqual(pool.fetch.await_args.args[2], 2)

    async def test_excluded_docs_are_filtered_and_the_limit_widened(self):
        pool = self.pool(["own", "a", "b"])
        token = knowledge.EXCLUDED_DOCS.set(frozenset({"own"}))
        try:
            hits = await knowledge.search("ki griefing", 2, pool=pool)
        finally:
            knowledge.EXCLUDED_DOCS.reset(token)
        self.assertEqual([hit["filename"] for hit in hits], ["a", "b"])
        self.assertEqual(pool.fetch.await_args.args[2], 3)


class RunDiffTests(unittest.TestCase):
    def run_of(self, metrics, passes, created="2026-11-12T00:00:00"):
        return {
            "kind": "retrieval",
            "created_at": created,
            "metrics": {"overall": metrics},
            "cases": [{"id": case_id, "pass": ok, "question": f"q{case_id}"} for case_id, ok in passes.items()],
        }

    def test_deltas_regressions_and_fixes(self):
        old = self.run_of({"n": 3, "mrr": 0.5}, {1: True, 2: False, 3: True})
        new = self.run_of({"n": 4, "mrr": 0.75, "new_metric": 1.0}, {1: False, 2: True, 3: True, 4: False})
        new["cases"].append({"id": 5, "error": "x"})
        diff = evals.diff_runs(old, new)
        self.assertEqual(diff["metrics"]["mrr"], {"old": 0.5, "new": 0.75, "delta": 0.25})
        self.assertNotIn("new_metric", diff["metrics"])
        self.assertEqual((diff["regressed"], diff["fixed"]), ([1], [2]))
        text = evals.format_diff(diff, new["cases"])
        self.assertIn("mrr: 0.500 -> 0.750 (+0.250)", text)
        self.assertIn("regressed (1):", text)
        self.assertIn("#1 q1", text)

    def test_first_run_has_nothing_to_compare(self):
        diff = evals.diff_runs(None, self.run_of({"n": 1}, {1: True}))
        self.assertEqual(diff["regressed"], [])
        self.assertEqual(evals.format_diff(diff), "no previous run to compare with")

    def test_run_files_round_trip_in_time_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = evals.save_run(self.run_of({"n": 1}, {}), directory=Path(tmp), now=datetime(2026, 11, 1, tzinfo=timezone.utc))
            second = evals.save_run(self.run_of({"n": 2}, {}), directory=Path(tmp), now=datetime(2026, 11, 12, tzinfo=timezone.utc))
            evals.save_run({**self.run_of({}, {}), "kind": "answers"}, directory=Path(tmp))
            self.assertEqual(evals.run_files("retrieval", Path(tmp)), [first, second])
            self.assertEqual(first.name, "20261101T000000Z-retrieval.json")
            self.assertEqual(evals.load_run(second)["metrics"]["overall"]["n"], 2)
        self.assertEqual(evals.run_files("retrieval", Path(tmp)), [])


class CaseBuilderTests(unittest.TestCase):
    def test_from_transcript(self):
        row = {
            "channel_id": 900,
            "title": "Ki blasts break blocks",
            "problem": "Ki blasts destroy builds on the server.",
            "resolution": "Set kiGriefing to false.",
            "knowledge_filename": "ticket-900.md",
            "language": "es",
            "category": "server",
        }
        built = eval_cases.case_from_transcript(row)
        self.assertEqual(built.source_ref, "ticket:900")
        self.assertEqual(built.question, "Ki blasts break blocks\nKi blasts destroy builds on the server.")
        self.assertEqual(built.reference, "Set kiGriefing to false.")
        self.assertEqual(built.exclude_docs, ("ticket-900.md",))
        self.assertEqual(built.language, "en")  # the summary text is English, so the replay is too
        self.assertEqual(built.category, "server")
        self.assertIsNone(built.expected_docs)

    def test_transcript_without_problem_or_resolution_is_skipped(self):
        base = {"channel_id": 1, "title": "t", "problem": "p p", "resolution": "r", "knowledge_filename": None}
        self.assertIsNone(eval_cases.case_from_transcript({**base, "problem": " "}))
        self.assertIsNone(eval_cases.case_from_transcript({**base, "resolution": None}))
        self.assertIsNone(eval_cases.case_from_transcript({**base, "resolution": "No resolution reached."}))
        built = eval_cases.case_from_transcript({**base, "title": "", "language": "pt"})
        self.assertEqual((built.question, built.exclude_docs, built.language), ("p p", (), "pt"))  # too short to tell: the ticket's language

    def test_from_support_info_is_labelled_with_no_docs(self):
        entry = SimpleNamespace(id=7, title="Where is the config?", body="config/dragonminez/ in the instance.")
        built = eval_cases.case_from_support_info(entry)
        self.assertEqual((built.source, built.source_ref, built.expected_docs), ("support_info", "support_info:7", ()))
        self.assertIsNone(eval_cases.case_from_support_info(SimpleNamespace(id=8, title="", body="x")))

    def test_from_feedback(self):
        built = eval_cases.case_from_feedback(55, " ¿Cómo desactivo el griefing? ", "Pon kiGriefing en false.", "es")
        self.assertEqual((built.source_ref, built.question, built.language), ("feedback:55", "¿Cómo desactivo el griefing?", "es"))
        self.assertIsNone(eval_cases.case_from_feedback(55, "", "x"))


class CaseStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_case_passes_labels_and_exclusions(self):
        pool = SimpleNamespace(fetchval=AsyncMock(return_value=3))
        built = eval_cases.NewCase("support_info", "support_info:7", "q", "r", expected_docs=())
        self.assertEqual(await eval_cases.add_case(built, pool=pool), 3)
        sql, *args = pool.fetchval.await_args.args
        self.assertIn("ON CONFLICT (source_ref)", sql)
        self.assertEqual(args, ["support_info", "support_info:7", "q", "r", "en", None, [], []])
        await eval_cases.add_case(eval_cases.NewCase("ticket", "ticket:1", "q", "r", exclude_docs=("d",)), pool=pool)
        self.assertEqual(pool.fetchval.await_args.args[7:], (["d"], None))

    async def test_list_cases_filters(self):
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        await eval_cases.list_cases(source="ticket", labelled=False, limit=5, pool=pool)
        sql, *args = pool.fetch.await_args.args
        self.assertIn("source = $1 AND active = $2 AND expected_docs IS NULL", sql)
        self.assertTrue(sql.endswith("LIMIT $3"))
        self.assertEqual(args, ["ticket", True, 5])
        await eval_cases.list_cases(with_docs=True, pool=pool)
        self.assertIn("cardinality(expected_docs) > 0", pool.fetch.await_args.args[0])

    async def test_hooks_never_raise(self):
        broken = SimpleNamespace(fetchrow=AsyncMock(side_effect=RuntimeError("db down")), execute=AsyncMock(side_effect=RuntimeError("db down")))
        with self.assertLogs(eval_cases.log, "ERROR"):
            await eval_cases.track_ticket(900, pool=broken)
            await eval_cases.untrack_ticket(900, pool=broken)

    async def test_track_ticket_adds_the_latest_transcript(self):
        row = {"channel_id": 900, "title": "T", "problem": "p", "resolution": "r", "knowledge_filename": "k.md", "language": "en", "category": None}
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row), fetchval=AsyncMock(return_value=1))
        await eval_cases.track_ticket(900, pool=pool)
        self.assertEqual(pool.fetchrow.await_args.args[1], 900)
        self.assertEqual(pool.fetchval.await_args.args[2], "ticket:900")

    async def test_track_feedback_uses_the_full_question(self):
        row = SimpleNamespace(language="pt")
        fixed = SimpleNamespace(question="Como desativo o griefing de ki no servidor?")
        with (
            patch("bulmaai.services.ai_feedback.get", AsyncMock(return_value=row)),
            patch("bulmaai.services.ai_feedback.answer_to_fix", AsyncMock(return_value=fixed)),
            patch.object(eval_cases, "add_case", AsyncMock()) as add,
        ):
            await eval_cases.track_feedback(55, "Use kiGriefing=false.")
        built = add.await_args.args[0]
        self.assertEqual((built.source_ref, built.question, built.reference, built.language), ("feedback:55", fixed.question, "Use kiGriefing=false.", "pt"))


class BotHookTests(unittest.IsolatedAsyncioTestCase):
    """Auto-close On/Off, Staff Bulma's solve and the Answers to fix submit hand eval_cases a background hook."""

    async def toggle(self, current, updated):
        tt = ticket_tests
        cog, channel = tt.make_cog(), tt.make_channel()
        cog.bot.get_cog = lambda name: None
        card = tt.views.closed_view(None, None, delete_at=current.delete_at, url="https://t/x", saving=False)
        interaction = tt.click(tt.member(tt.STAFF_ID, tt.HELPER_ROLE), tt.views.AUTOCLOSE_BUTTON_ID, card)
        interaction.channel = channel
        with (
            patch.object(tt.cog_module, "get_ticket_by_channel", AsyncMock(return_value=current)),
            patch.object(tt.cog_module, "schedule_delete", AsyncMock(return_value=updated)),
            patch.object(tt.cog_module, "cancel_delete", AsyncMock(return_value=updated)),
            patch.object(tt.cog_module, "set_transcript_resolved", AsyncMock()),
            patch.object(tt.cog_module, "get_page_for_channel", AsyncMock(return_value=None)),
            patch.object(eval_cases, "spawn") as spawn,
            patch.object(eval_cases, "track_ticket", MagicMock(return_value="track")) as track,
            patch.object(eval_cases, "untrack_ticket", MagicMock(return_value="untrack")) as untrack,
        ):
            await cog.on_interaction(interaction)
        return spawn, track, untrack

    async def test_auto_close_on_tracks_and_off_untracks(self):
        later = datetime(2099, 1, 1, tzinfo=timezone.utc)
        make = ticket_tests.make_ticket
        spawn, track, untrack = await self.toggle(make(status="closed"), make(status="closed", delete_at=later))
        track.assert_called_once_with(900)
        untrack.assert_not_called()
        spawn.assert_called_once_with("track")
        spawn, track, untrack = await self.toggle(make(status="closed", delete_at=later), make(status="closed"))
        untrack.assert_called_once_with(900)
        track.assert_not_called()
        spawn.assert_called_once_with("untrack")

    async def solve(self, staff):
        tt = ticket_tests
        owner = SimpleNamespace(id=tt.OWNER_ID, roles=[], send=AsyncMock())
        cog, channel = tt.make_cog(), tt.make_channel(owner)
        cog.bot.get_cog = lambda name: None
        with (
            patch.object(tt.cog_module, "mark_closed", AsyncMock(return_value=tt.make_ticket(status="closed"))),
            patch.object(cog, "_archive", AsyncMock(return_value=True)),
            patch.object(tt.cog_module, "get_page_for_channel", AsyncMock(return_value=None)),
            patch.object(cog, "_set_row", AsyncMock()),
            patch.object(eval_cases, "spawn") as spawn,
            patch.object(eval_cases, "track_ticket", MagicMock(return_value="track")) as track,
        ):
            await cog.solve_ticket(channel, closer_id=tt.STAFF_ID if staff else tt.OWNER_ID, staff=staff)
        return spawn, track

    async def test_staff_bulma_solve_tracks_but_the_owner_solve_does_not(self):
        spawn, track = await self.solve(staff=True)
        track.assert_called_once_with(900)
        spawn.assert_called_once_with("track")
        spawn, track = await self.solve(staff=False)
        track.assert_not_called()
        spawn.assert_not_called()

    async def submit(self, add):
        ff = fix_tests
        cog = ff.make_cog()
        interaction = ff.interaction_for(ff.staff_member(ff.STAFF_ROLE), "fbfix_modal:55", modal_components=ff.form_payload())
        with (
            patch.object(ff.feedback_store, "claim_review", AsyncMock(return_value=True)),
            patch.object(ff.feedback_store, "release_review", AsyncMock()),
            patch.object(ff.feedback_store, "set_info", AsyncMock()),
            patch.object(ff.db, "add_info", add),
            patch.object(eval_cases, "spawn") as spawn,
            patch.object(eval_cases, "track_feedback", MagicMock(return_value="fb")) as track,
        ):
            await cog.on_interaction(interaction)
        return spawn, track

    async def test_answers_to_fix_submit_tracks_the_staff_answer(self):
        spawn, track = await self.submit(AsyncMock(return_value=fix_tests.info_entry()))
        track.assert_called_once_with(55, "Set kiGriefing to false.")
        spawn.assert_called_once_with("fb")

    async def test_a_rejected_answer_tracks_nothing(self):
        spawn, track = await self.submit(AsyncMock(side_effect=ValueError("too long")))
        track.assert_not_called()
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
