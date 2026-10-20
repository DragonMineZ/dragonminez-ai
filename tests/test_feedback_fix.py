import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import support_info as cog_module
from bulmaai.services import ai_feedback as feedback_store
from bulmaai.services import support_info as db
from bulmaai.services import support_traces
from bulmaai.ui import feedback_fix
from bulmaai.ui.v2 import ALREADY_WORKING, TEXT_LIMIT
from v2_helpers import buttons, sent_message, text, texts

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
STAFF_ROLE = 7


def answer(**overrides) -> feedback_store.AnswerToFix:
    values = dict(
        message_id=55,
        channel_id=900,
        language="es",
        created_at=NOW - timedelta(days=2),
        question="How do I turn off Ki blast griefing?",
        reply="Set `kiGriefing` to false in general-server.json.",
    )
    values.update(overrides)
    return feedback_store.AnswerToFix(**values)


def feedback_row(**overrides) -> feedback_store.FeedbackRow:
    values = dict(
        message_id=55,
        channel_id=900,
        requester_id=42,
        response_id="msg_1",
        helpful=False,
        language="es",
        created_at=NOW - timedelta(days=2),
    )
    values.update(overrides)
    return feedback_store.FeedbackRow(**values)


def info_entry(**overrides) -> db.SupportInfo:
    values = dict(
        id=12,
        kind="permanent",
        title="Ki blast griefing",
        body="Set kiGriefing to false.",
        status="active",
        source="feedback",
        ticket_id=None,
        expires_at=None,
        created_by=300,
        updated_by=300,
        review_channel_id=None,
        review_message_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(overrides)
    return db.SupportInfo(**values)


def trace(response_id="msg_1", **overrides) -> dict:
    values = {
        "id": 3,
        "created_at": NOW,
        "response_id": response_id,
        "workflow": "support_question",
        "language": "es",
        "channel_id": 900,
        "user_id": 42,
        "reply_text": "Set it to false.",
        "input_json": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "[requester Goku · 5m ago] How do I stop griefing?"},
        ],
    }
    values.update(overrides)
    return values


def fake_pool(**methods) -> MagicMock:
    pool = MagicMock()
    for name, value in methods.items():
        setattr(pool, name, AsyncMock(return_value=value))
    return pool


# ---- services ----------------------------------------------------------------------------------------------------


class TraceLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_looks_up_sent_replies_only_and_decodes_the_input(self) -> None:
        conn = MagicMock()
        stored = {**trace(), "input_json": json.dumps(trace()["input_json"])}  # asyncpg hands jsonb back as text
        conn.fetch = AsyncMock(return_value=[stored])
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        found = await support_traces.get_reply_traces(["msg_1", "msg_1", None, "msg_2"], pool=pool)
        query, ids = conn.fetch.await_args.args
        self.assertIn("workflow IN ('support_question', 'support_escalation')", query)
        self.assertIn("response_id = ANY($1::text[])", query)
        self.assertIn("DISTINCT ON (response_id)", query)
        self.assertEqual(ids, ["msg_1", "msg_2"])
        self.assertEqual(list(found), ["msg_1"])
        self.assertEqual(found["msg_1"]["input_json"][1]["role"], "user")

    async def test_no_ids_no_query(self) -> None:
        pool = MagicMock()
        self.assertEqual(await support_traces.get_reply_traces([None, ""], pool=pool), {})
        pool.acquire.assert_not_called()


class FeedbackReviewStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_downvoted_reads_the_weeks_unreviewed_thumbs_down(self) -> None:
        record = {
            "message_id": 55,
            "channel_id": 900,
            "requester_id": 42,
            "response_id": "msg_1",
            "helpful": False,
            "language": "es",
            "created_at": NOW,
        }  # a row from before the review columns still reads
        pool = fake_pool(fetch=[record])
        with patch.object(feedback_store, "get_pool", AsyncMock(return_value=pool)):
            rows = await feedback_store.downvoted(NOW - timedelta(days=7), NOW, limit=10)
        query, *args = pool.fetch.await_args.args
        for part in ("helpful = false", "response_id IS NOT NULL", "reviewed_at IS NULL", "ORDER BY created_at DESC LIMIT $3"):
            self.assertIn(part, query)
        self.assertEqual(args, [NOW - timedelta(days=7), NOW, 10])
        self.assertEqual((rows[0].message_id, rows[0].reviewed_at, rows[0].info_id), (55, None, None))

    async def test_answers_to_fix_pairs_rows_with_their_trace(self) -> None:
        rows = [feedback_row(), feedback_row(message_id=56, response_id="msg_gone")]
        with (
            patch.object(feedback_store, "downvoted", AsyncMock(return_value=rows)),
            patch.object(feedback_store, "get_reply_traces", AsyncMock(return_value={"msg_1": trace()})) as lookup,
        ):
            items = await feedback_store.answers_to_fix(NOW - timedelta(days=7), NOW)
        lookup.assert_awaited_once_with(["msg_1", "msg_gone"])
        self.assertEqual(len(items), 1)  # the answer whose trace is gone has nothing to show
        self.assertEqual(
            (items[0].message_id, items[0].question, items[0].reply, items[0].language),
            (55, "How do I stop griefing?", "Set it to false.", "es"),
        )

    async def test_claim_release_and_link(self) -> None:
        pool = fake_pool(fetchval=55, execute=None)
        with patch.object(feedback_store, "get_pool", AsyncMock(return_value=pool)):
            self.assertTrue(await feedback_store.claim_review(55, 300))
            pool.fetchval.return_value = None
            self.assertFalse(await feedback_store.claim_review(55, 301))
            await feedback_store.release_review(55, 300)
            release_query, *release_args = pool.execute.await_args.args
            await feedback_store.set_info(55, 12)
        claim_query = pool.fetchval.await_args.args[0]
        self.assertIn("SET reviewed_at = now(), reviewed_by = $2", claim_query)
        self.assertIn("WHERE message_id = $1 AND reviewed_at IS NULL", claim_query)
        self.assertIn("reviewed_by = $2 AND info_id IS NULL", release_query)  # only your own, unfinished claim
        self.assertEqual(release_args, [55, 300])
        self.assertEqual(pool.execute.await_args.args[1:], (55, 12))


# ---- cards -------------------------------------------------------------------------------------------------------


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_item_card_shows_where_and_both_excerpts_with_routed_buttons(self) -> None:
        view = feedback_fix.item_card(answer(), "<#900>")
        out = text(view)
        self.assertIn("**Where** <#900>", out)
        self.assertIn("**Language** es", out)
        self.assertIn(f"<t:{int((NOW - timedelta(days=2)).timestamp())}:d>", out)
        self.assertIn("**Question**\n> How do I turn off Ki blast griefing?", out)
        self.assertIn("**Bulma's answer**\n> Set \\`kiGriefing\\` to false", out)
        ids = [(button.label, button.custom_id) for button in buttons(view)]
        self.assertEqual(ids, [("Write the right answer", "fbfix:write:55"), ("Skip", "fbfix:skip:55")])
        self.assertEqual(str(buttons(view)[0].emoji), "✏️")

    async def test_excerpts_are_trimmed_and_inert(self) -> None:
        question = "# Help @everyone <@123> see https://evil.tld/x and [here](https://evil.tld/y)\n\n" + "q" * 900
        reply = "-# **Bold** <@&9> " + "r" * 900
        out = text(feedback_fix.item_card(answer(question=question, reply=reply), "DM"))
        question_line = next(line for line in out.split("\n") if line.startswith("> \\#"))
        reply_line = next(line for line in out.split("\n") if line.startswith("> \\-#"))
        self.assertLessEqual(len(question_line), feedback_fix.QUESTION_EXCERPT + 40)
        self.assertLessEqual(len(reply_line), feedback_fix.REPLY_EXCERPT + 40)
        self.assertTrue(question_line.endswith("…") and reply_line.endswith("…"))
        self.assertNotIn("<@123>", out)
        self.assertNotIn("<@&9>", out)
        self.assertNotIn("@everyone", out)  # defused with a zero-width space
        self.assertNotIn("https://", out)  # no clickable links, no previews
        self.assertNotIn("](", out)
        self.assertIn("\\*\\*Bold\\*\\*", out)
        self.assertIn("**Where** DM", out)
        self.assertLess(len(out), TEXT_LIMIT)

    async def test_empty_texts_and_header(self) -> None:
        out = text(feedback_fix.item_card(answer(question="", reply="  "), "<#1>"))
        self.assertEqual(out.count("(no text)"), 2)
        self.assertIn("3 answers rated 👎", text(feedback_fix.header_card(3)))
        self.assertIn("1 answer rated 👎", text(feedback_fix.header_card(1)))
        self.assertEqual(buttons(feedback_fix.header_card(3)), [])

    async def test_collapsed_cards(self) -> None:
        fixed = feedback_fix.fixed_card(300, 12, "Ki @everyone griefing")
        self.assertEqual(texts(fixed), ["✅ <@300> → support info #12: Ki @​everyone griefing"])
        self.assertEqual(buttons(fixed), [])
        self.assertEqual(texts(feedback_fix.skipped_card(301)), ["⏭️ Skipped by <@301>"])
        self.assertEqual(texts(feedback_fix.handled_card(301, None)), ["⏭️ Skipped by <@301>"])
        self.assertEqual(texts(feedback_fix.handled_card(300, 12, "T")), ["✅ <@300> → support info #12: T"])

    def test_custom_ids_round_trip(self) -> None:
        self.assertEqual(feedback_fix.parse_custom_id("fbfix:write:55"), ("write", 55))
        self.assertEqual(feedback_fix.parse_custom_id("fbfix:skip:55"), ("skip", 55))
        for bad in ("fbfix:delete:55", "fbfix:write:x", "fbfix_modal:55", "supinfo:approve:5", "ai_fb:1:down"):
            self.assertIsNone(feedback_fix.parse_custom_id(bad))
        self.assertEqual(feedback_fix.parse_modal_id(feedback_fix.modal_id(55)), 55)
        self.assertIsNone(feedback_fix.parse_modal_id("fbfix:write:55"))
        self.assertIsNone(feedback_fix.parse_modal_id("fbfix_modal:abc"))

    async def test_modal_is_prefilled_and_routed_by_custom_id(self) -> None:
        modal = feedback_fix.fix_modal(55, "  How do\nI <@1> fix " + "x" * 200)
        self.assertEqual((modal.custom_id, modal.title), ("fbfix_modal:55", "Write the right answer"))
        fields = {label.item.custom_id: label.item for label in modal.children}
        title = fields[feedback_fix.FIELD_TITLE].value
        self.assertTrue(title.startswith("How do I fix x"))
        self.assertLessEqual(len(title), db.TITLE_MAX)
        self.assertEqual(fields[feedback_fix.FIELD_ANSWER].max_length, db.BODY_MAX)
        self.assertTrue(fields[feedback_fix.FIELD_ANSWER].required)
        self.assertEqual([o.value for o in fields[feedback_fix.FIELD_KIND].options if o.default], ["permanent"])
        self.assertFalse(fields[feedback_fix.FIELD_DAYS].required)
        self.assertIsNone(feedback_fix.fix_modal(55).children[0].item.value)


def form_payload(*, title="Ki griefing", answer_text="Set kiGriefing to false.", kind="permanent", days=""):
    return [
        {"type": 18, "component": {"type": 4, "custom_id": "title", "value": title}},
        {"type": 18, "component": {"type": 4, "custom_id": "answer", "value": answer_text}},
        {"type": 18, "component": {"type": 3, "custom_id": "kind", "values": [kind] if kind else []}},
        {"type": 18, "component": {"type": 4, "custom_id": "days", "value": days}},
    ]


class FormTests(unittest.TestCase):
    def test_reads_the_submitted_values(self) -> None:
        form = feedback_fix.read_form(form_payload(kind="temporary", days=" 3 "))
        self.assertEqual(form, feedback_fix.FixForm("Ki griefing", "Set kiGriefing to false.", "temporary", "3"))
        self.assertEqual(feedback_fix.read_form(form_payload(kind="")).kind, "permanent")
        self.assertEqual(feedback_fix.read_form(None), feedback_fix.FixForm("", "", "permanent", ""))


# ---- routing, gates and claims (cogs/support_info.py) -----------------------------------------------------------


def make_cog():
    bot = MagicMock()
    bot.settings = SimpleNamespace(discord_staff_role_ids=(STAFF_ROLE,), panel_guild_id=1, dev_guild_id=None)
    return cog_module.SupportInfoCog(bot)


def staff_member(*role_ids, guild_id=1, user_id=300):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
    member.guild = SimpleNamespace(id=guild_id)
    return member


def interaction_for(user, custom_id: str, *, modal_components=None, with_message=True):
    interaction = MagicMock()
    interaction.type = (
        discord.InteractionType.modal_submit if modal_components is not None else discord.InteractionType.component
    )
    interaction.data = {"custom_id": custom_id}
    if modal_components is not None:
        interaction.data["components"] = modal_components
    interaction.user = user
    interaction.message = sent_message(feedback_fix.item_card(answer(), "<#900>"), message_id=801, channel_id=800) if with_message else None
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_staff_and_other_servers_are_denied(self) -> None:
        cog = make_cog()
        for user in (staff_member(), staff_member(STAFF_ROLE, guild_id=99), SimpleNamespace(id=1)):
            for custom_id in ("fbfix:write:55", "fbfix:skip:55"):
                interaction = interaction_for(user, custom_id)
                with (
                    patch.object(feedback_store, "claim_review", AsyncMock()) as claim,
                    patch.object(feedback_store, "get", AsyncMock()) as get,
                ):
                    await cog.on_interaction(interaction)
                claim.assert_not_awaited()
                get.assert_not_awaited()
                interaction.response.send_modal.assert_not_awaited()
                interaction.response.send_message.assert_awaited_once_with(cog_module.STAFF_ONLY_MSG, ephemeral=True)

    async def test_write_opens_the_form_with_the_question_as_title(self) -> None:
        cog = make_cog()
        interaction = interaction_for(staff_member(STAFF_ROLE), "fbfix:write:55")
        with (
            patch.object(feedback_store, "get", AsyncMock(return_value=feedback_row())) as get,
            patch.object(feedback_store, "answer_to_fix", AsyncMock(return_value=answer())),
            patch.object(feedback_store, "claim_review", AsyncMock()) as claim,
        ):
            await cog.on_interaction(interaction)
        get.assert_awaited_once_with(55)
        claim.assert_not_awaited()  # opening the form claims nothing; the submit does
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual(modal.custom_id, "fbfix_modal:55")
        self.assertEqual(modal.children[0].item.value, "How do I turn off Ki blast griefing?")

    async def test_write_without_a_trace_still_opens_an_empty_form(self) -> None:
        cog = make_cog()
        interaction = interaction_for(staff_member(STAFF_ROLE), "fbfix:write:55")
        with (
            patch.object(feedback_store, "get", AsyncMock(return_value=feedback_row())),
            patch.object(feedback_store, "answer_to_fix", AsyncMock(return_value=None)),
        ):
            await cog.on_interaction(interaction)
        self.assertIsNone(interaction.response.send_modal.await_args.args[0].children[0].item.value)

    async def test_write_on_a_handled_answer_shows_the_outcome(self) -> None:
        cog = make_cog()
        interaction = interaction_for(staff_member(STAFF_ROLE), "fbfix:write:55")
        handled = feedback_row(reviewed_at=NOW, reviewed_by=301, info_id=12)
        with (
            patch.object(feedback_store, "get", AsyncMock(return_value=handled)),
            patch.object(db, "get_info", AsyncMock(return_value=info_entry())),
        ):
            await cog.on_interaction(interaction)
        interaction.response.send_modal.assert_not_awaited()
        view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(texts(view), ["✅ <@301> → support info #12: Ki blast griefing"])
        interaction.followup.send.assert_awaited_once_with(cog_module.FIX_HANDLED_MSG, ephemeral=True)

    async def test_skip_claims_and_collapses(self) -> None:
        cog = make_cog()
        interaction = interaction_for(staff_member(STAFF_ROLE), "fbfix:skip:55")
        with patch.object(feedback_store, "claim_review", AsyncMock(return_value=True)) as claim:
            await cog.on_interaction(interaction)
        claim.assert_awaited_once_with(55, 300)
        interaction.response.edit_message.assert_awaited_once()  # the busy state first (while_busy)
        view = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertEqual(texts(view), ["⏭️ Skipped by <@300>"])
        self.assertEqual(buttons(view), [])
        interaction.followup.send.assert_not_awaited()

    async def test_skip_after_someone_else_shows_their_outcome(self) -> None:
        cog = make_cog()
        interaction = interaction_for(staff_member(STAFF_ROLE), "fbfix:skip:55")
        with (
            patch.object(feedback_store, "claim_review", AsyncMock(return_value=False)),
            patch.object(feedback_store, "get", AsyncMock(return_value=feedback_row(reviewed_at=NOW, reviewed_by=301))),
        ):
            await cog.on_interaction(interaction)
        self.assertEqual(texts(interaction.edit_original_response.await_args.kwargs["view"]), ["⏭️ Skipped by <@301>"])
        interaction.followup.send.assert_awaited_once_with(cog_module.FIX_HANDLED_MSG, ephemeral=True)

    async def test_double_click_while_working(self) -> None:
        cog = make_cog()
        cog._working.add(801)
        interaction = interaction_for(staff_member(STAFF_ROLE), "fbfix:skip:55")
        with patch.object(feedback_store, "claim_review", AsyncMock()) as claim:
            await cog.on_interaction(interaction)
        claim.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(ALREADY_WORKING, ephemeral=True)


class SubmitTests(unittest.IsolatedAsyncioTestCase):
    async def submit(self, components, *, claimed=True, add=None, user=None, with_message=True):
        cog = make_cog()
        interaction = interaction_for(
            user or staff_member(STAFF_ROLE), "fbfix_modal:55", modal_components=components, with_message=with_message
        )
        add = add or AsyncMock(return_value=info_entry())
        with (
            patch.object(feedback_store, "claim_review", AsyncMock(return_value=claimed)) as claim,
            patch.object(feedback_store, "release_review", AsyncMock()) as release,
            patch.object(feedback_store, "set_info", AsyncMock()) as set_info,
            patch.object(db, "add_info", add),
        ):
            await cog.on_interaction(interaction)
        return interaction, claim, release, set_info, add

    async def test_saves_live_support_info_and_collapses_the_card(self) -> None:
        interaction, claim, release, set_info, add = await self.submit(form_payload())
        claim.assert_awaited_once_with(55, 300)
        self.assertEqual(
            add.await_args.kwargs,
            {
                "kind": "permanent",
                "title": "Ki griefing",
                "body": "Set kiGriefing to false.",
                "created_by": 300,
                "expires_at": None,
                "status": "active",
                "source": "feedback",
            },
        )
        set_info.assert_awaited_once_with(55, 12)
        release.assert_not_awaited()
        view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(texts(view), ["✅ <@300> → support info #12: Ki blast griefing"])

    async def test_temporary_with_days_expires_and_permanent_ignores_days(self) -> None:
        _, _, _, _, add = await self.submit(form_payload(kind="temporary", days="3"))
        expires_at = add.await_args.kwargs["expires_at"]
        self.assertAlmostEqual((expires_at - datetime.now(timezone.utc)).total_seconds(), 3 * 86400, delta=60)
        _, _, _, _, add = await self.submit(form_payload(kind="permanent", days="3"))
        self.assertIsNone(add.await_args.kwargs["expires_at"])
        _, _, _, _, add = await self.submit(form_payload(kind="temporary", days=""))
        self.assertIsNone(add.await_args.kwargs["expires_at"])

    async def test_bad_days_are_refused_before_claiming(self) -> None:
        for days in ("abc", "0", "400"):
            interaction, claim, _, _, add = await self.submit(form_payload(kind="temporary", days=days))
            claim.assert_not_awaited()
            add.assert_not_awaited()
            interaction.response.send_message.assert_awaited_once_with(cog_module.DAYS_MSG, ephemeral=True)

    async def test_lost_claim_saves_nothing(self) -> None:
        interaction, _, release, _, add = await self.submit(form_payload(), claimed=False)
        add.assert_not_awaited()
        release.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(cog_module.FIX_HANDLED_MSG, ephemeral=True)
        interaction.response.edit_message.assert_not_awaited()

    async def test_validation_error_releases_the_claim(self) -> None:
        add = AsyncMock(side_effect=ValueError("Text is too long (1600/1500 characters)."))
        interaction, _, release, set_info, _ = await self.submit(form_payload(), add=add)
        release.assert_awaited_once_with(55, 300)
        set_info.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            "Text is too long (1600/1500 characters).", ephemeral=True
        )
        interaction.response.edit_message.assert_not_awaited()

    async def test_db_error_releases_and_says_try_again(self) -> None:
        add = AsyncMock(side_effect=RuntimeError("db down"))
        with self.assertLogs(cog_module.log, level="ERROR"):
            interaction, _, release, _, _ = await self.submit(form_payload(), add=add)
        release.assert_awaited_once_with(55, 300)
        interaction.response.send_message.assert_awaited_once_with(cog_module.FIX_FAILED_MSG, ephemeral=True)

    async def test_submit_re_checks_the_gate(self) -> None:
        interaction, claim, _, _, add = await self.submit(form_payload(), user=staff_member())
        claim.assert_not_awaited()
        add.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(cog_module.STAFF_ONLY_MSG, ephemeral=True)

    async def test_without_the_card_it_confirms_ephemerally(self) -> None:
        interaction, *_ = await self.submit(form_payload(), with_message=False)
        interaction.response.send_message.assert_awaited_once_with("Saved as support info #12.", ephemeral=True)

    async def test_other_modals_are_ignored(self) -> None:
        cog = make_cog()
        interaction = interaction_for(staff_member(STAFF_ROLE), "staff_draft_edit:5", modal_components=[])
        with patch.object(feedback_store, "claim_review", AsyncMock()) as claim:
            await cog.on_interaction(interaction)
        claim.assert_not_awaited()
        interaction.response.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
