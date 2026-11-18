import asyncio
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs import bug_reports as bug_cog
from bulmaai.cogs.bug_reports import BugReportsCog
from bulmaai.services import code_notes, notion, staff_tools
from bulmaai.services.bug_report_ai import BugTriage
from bulmaai.web.core import Tier

PAGE = "0123456789abcdef0123456789abcdef"
PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"


class BodyTests(unittest.TestCase):
    def test_bug_item_is_an_inbox_mod_bug_with_priority_from_severity(self) -> None:
        body = notion.bug_item(
            "ds-1", title="Crash on load", summary="x" * 3000, severity="critical",
            reporter="goku (Discord)", thread_url="https://discord.com/channels/1/2", github="https://gh/1",
        )
        props = body["properties"]
        self.assertEqual(body["parent"], {"type": "data_source_id", "data_source_id": "ds-1"})
        self.assertEqual(props["Status"], {"status": {"name": "Inbox"}})
        self.assertEqual(props["Type"]["select"]["name"], "Bug")
        self.assertEqual(props["Project"]["select"]["name"], "Mod")
        self.assertEqual(props["Priority"]["select"]["name"], "P0 Critical")
        self.assertEqual(props["GitHub"], {"url": "https://gh/1"})
        self.assertEqual(len(body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]), 2000)

    def test_unknown_severity_is_normal(self) -> None:
        body = notion.bug_item("d", title="", summary="", severity="weird", reporter="", thread_url="u", github=None)
        self.assertEqual(body["properties"]["Priority"]["select"]["name"], "P2 Normal")
        self.assertNotIn("GitHub", body["properties"])

    def test_item_update_only_sends_what_changed(self) -> None:
        self.assertEqual(notion.item_update(), {"properties": {}})
        self.assertEqual(notion.item_update(status="Done"), {"properties": {"Status": {"status": {"name": "Done"}}}})

    def test_tracker_filter_hides_done_unless_asked(self) -> None:
        self.assertEqual(len(notion.tracker_filter()["and"]), 2)
        self.assertIsNone(notion.tracker_filter(include_done=True))
        self.assertEqual(notion.tracker_filter(status="Done"), {"property": "Status", "status": {"equals": "Done"}})

    def test_flatten_reads_common_property_types(self) -> None:
        self.assertEqual(notion.flatten({"type": "status", "status": {"name": "Inbox"}}), "Inbox")
        self.assertEqual(notion.flatten({"type": "unique_id", "unique_id": {"prefix": "DMZ", "number": 7}}), "DMZ-7")
        self.assertEqual(notion.flatten({"type": "title", "title": [{"plain_text": "A"}, {"plain_text": "B"}]}), "AB")


class ResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_links_and_ids_resolve_without_a_call(self) -> None:
        with patch.object(notion, "_call", AsyncMock()) as call:
            self.assertEqual(await notion.resolve_page("t", "ds", f"https://www.notion.so/Thing-{PAGE}"), PAGE_ID)
            self.assertEqual(await notion.resolve_page("t", "ds", PAGE_ID), PAGE_ID)
        call.assert_not_awaited()

    async def test_tracker_ids_are_looked_up(self) -> None:
        with patch.object(notion, "_call", AsyncMock(return_value={"results": [{"id": "page-12"}]})) as call:
            self.assertEqual(await notion.resolve_page("t", "ds", "DMZ-12"), "page-12")
        self.assertEqual(call.await_args.args[3]["filter"]["unique_id"], {"equals": 12})
        with patch.object(notion, "_call", AsyncMock(return_value={"results": []})):
            with self.assertRaises(notion.NotionError):
                await notion.resolve_page("t", "ds", "dmz-99")
        with self.assertRaises(notion.NotionError):
            await notion.resolve_page("t", "ds", "the crash one")


class BugReportHookTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, token: str | None = "secret"):
        cog = BugReportsCog.__new__(BugReportsCog)
        cog.bot = SimpleNamespace()
        cog.settings = SimpleNamespace(notion_token=token, notion_tracker_data_source_id="ds-1")
        cog._notion_tasks = set()
        return cog

    async def _drain(self, cog) -> None:
        await asyncio.gather(*cog._notion_tasks)

    async def test_confirmed_bug_becomes_a_tracker_item_once(self) -> None:
        cog = self._cog()
        interaction = SimpleNamespace(guild=SimpleNamespace(get_member=lambda _id: None), guild_id=1)
        report = SimpleNamespace(notion_page_id=None, reporter_id=42, guild_id=1, thread_id=9)
        triage = BugTriage(is_bug=True, title="Crash", summary="NPE", severity="high", affected_area="")
        with patch.object(notion, "create_page", AsyncMock(return_value={"id": "page-1"})) as create, \
                patch.object(bug_cog, "set_notion_page", AsyncMock()) as store:
            cog._notion(cog._notion_create(interaction, report, triage, "https://gh/5"))
            await self._drain(cog)
            report.notion_page_id = "page-1"
            cog._notion(cog._notion_create(interaction, report, triage, "https://gh/5"))
            await self._drain(cog)
        create.assert_awaited_once()
        body = create.await_args.args[1]
        self.assertEqual(body["properties"]["GitHub"], {"url": "https://gh/5"})
        self.assertEqual(body["properties"]["Reported by"]["rich_text"][0]["text"]["content"], "Discord user 42")
        store.assert_awaited_once_with(9, "page-1")

    async def test_final_statuses_reach_notion_and_others_do_not(self) -> None:
        cog = self._cog()
        report = SimpleNamespace(notion_page_id="page-1")
        with patch.object(bug_cog, "set_status", AsyncMock()), \
                patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=report)), \
                patch.object(notion, "update_page", AsyncMock()) as update:
            await cog._set_status(9, "resolved")
            await cog._set_status(9, "dismissed")
            await cog._set_status(9, "tracked")
            await self._drain(cog)
        self.assertEqual(sorted(call.kwargs["status"] for call in update.await_args_list), ["Done", "Won't do"])

    async def test_nothing_runs_without_a_token(self) -> None:
        cog = self._cog(token=None)
        with patch.object(bug_cog, "set_status", AsyncMock()) as set_status:
            await cog._set_status(9, "resolved")
        set_status.assert_awaited_once_with(9, "resolved")
        self.assertEqual(cog._notion_tasks, set())

    async def test_notion_failure_never_raises(self) -> None:
        cog = self._cog()
        with patch.object(bug_cog, "get_bug_report", AsyncMock(side_effect=RuntimeError("db down"))):
            await cog._notion_update(9, status="Done")


class StaffToolTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, token: str | None = "secret"):
        return SimpleNamespace(
            settings=SimpleNamespace(notion_token=token, notion_tracker_data_source_id="ds-1"),
            tier=Tier.HELPER, member=SimpleNamespace(id=5), guild=SimpleNamespace(id=1), origin="discord",
        )

    async def test_reads_run_at_once(self) -> None:
        with patch.object(notion, "query_tracker", AsyncMock(return_value=[{"title": "Crash"}])) as query:
            result = await staff_tools.run_tool("notion_tracker", {"status": "in progress"}, self._ctx())
        self.assertEqual(result, {"items": [{"title": "Crash"}]})
        self.assertEqual(query.await_args.kwargs["status"], "In progress")

    async def test_writes_are_only_proposed(self) -> None:
        with patch.object(staff_tools.staff_store, "create_action", AsyncMock(return_value=3)) as create, \
                patch.object(notion, "create_page", AsyncMock()) as notion_create:
            result = await staff_tools.run_tool(
                "notion_create_item", {"title": "Shockbyte code", "type": "task", "priority": "P1"}, self._ctx()
            )
        self.assertEqual(result["pending_action"], 3)
        notion_create.assert_not_awaited()
        stored = create.await_args.args[3]
        self.assertEqual((stored["item_type"], stored["priority"]), ("Task", "P1 High"))
        self.assertIn("notion_create_item", staff_tools.ACTION_TOOLS)

    async def test_bad_option_and_missing_token_are_errors(self) -> None:
        bad = await staff_tools.run_tool("notion_tracker", {"status": "Shipped"}, self._ctx())
        self.assertIn("must be one of", bad["error"])
        off = await staff_tools.run_tool("notion_search", {"query": "x"}, self._ctx(token=None))
        self.assertIn("isn't connected", off["error"])

    async def test_confirmed_update_runs_on_notion(self) -> None:
        member = SimpleNamespace(display_name="Bruno")
        args = {"page_id": "p1", "label": "DMZ-3", "status": "Done", "priority": None, "title": None}
        settings = SimpleNamespace(notion_token="secret", notion_tracker_data_source_id="ds-1")
        with patch.object(notion, "update_page", AsyncMock(return_value={"id": "p1", "url": "https://notion.so/p1"})) as update:
            text = await staff_tools._execute_notion("notion_update_item", args, settings, member)
        update.assert_awaited_once_with("secret", "p1", status="Done", priority=None, title=None)
        self.assertIn("https://notion.so/p1", text)


def _commit(sha: str, message: str, branch: str = "main"):
    return code_notes.PushCommit(sha=sha, branch=branch, message=message, author_login="goku", author_name="Goku",
                                 committed_at=datetime(2026, 11, 18, tzinfo=timezone.utc))


PAYLOAD = {"repository": {"full_name": "DragonMineZ/dragonminez", "default_branch": "main"},
           "pusher": {"name": "bruno"}, "compare": "https://github.com/c"}


class PushLogTests(unittest.IsolatedAsyncioTestCase):
    def test_kind_and_refs(self) -> None:
        self.assertEqual(notion.push_kind("fix ticket sweep"), "🐛 Fix")
        self.assertEqual(notion.push_kind("tests for ai_guard"), "🧪 Tests")
        self.assertEqual(notion.push_kind("refactor stuff"), "🔧 Change")
        refs, closes = notion.push_refs(["work on DMZ-3", "fixes dmz-7 and DMZ-3"])
        self.assertEqual((refs, closes), ([3, 7], {7}))

    def test_entry_has_title_props_and_linked_commits(self) -> None:
        commits = [_commit("a" * 40, "first"), _commit("b" * 40, "fix crash\n\nbody")]
        body = notion.push_entry("cl", PAYLOAD, commits, project="Mod", item_ids=["p1"])
        props = body["properties"]
        self.assertEqual(props["Name"]["title"][0]["text"]["content"], "fix crash (+1 more)")
        self.assertEqual(props["Kind"]["select"]["name"], "🐛 Fix")
        self.assertEqual(props["Commits"]["number"], 2)
        self.assertEqual(props["Items"]["relation"], [{"id": "p1"}])
        link = body["children"][0]["bulleted_list_item"]["rich_text"][0]["text"]["link"]["url"]
        self.assertEqual(link, "https://github.com/DragonMineZ/dragonminez/commit/" + "b" * 40)

    async def test_fixes_on_default_branch_mark_done(self) -> None:
        async def resolve(_token, _data_source, ref):
            if ref != "DMZ-7":
                raise notion.NotionError("missing")
            return "p7"

        with patch.object(notion, "resolve_page", resolve), \
                patch.object(notion, "create_page", AsyncMock()) as create, \
                patch.object(notion, "update_page", AsyncMock()) as update:
            await notion.log_push("t", "cl", "tr", PAYLOAD, [_commit("a" * 40, "fixes DMZ-7, see DMZ-99")], project="Mod")
            await notion.log_push("t", "cl", "tr", PAYLOAD, [_commit("b" * 40, "fixes DMZ-7", branch="wip")], project="Mod")
        self.assertEqual(create.await_count, 2)
        self.assertEqual(create.await_args_list[0].args[1]["properties"]["Items"]["relation"], [{"id": "p7"}])
        update.assert_awaited_once_with("t", "p7", status="Done")

    def test_off_without_token_or_changelog(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            for settings in (SimpleNamespace(notion_token=None, notion_changelog_data_source_id="cl"),
                             SimpleNamespace(notion_token="t", notion_changelog_data_source_id=None)):
                with patch.object(asyncio, "run_coroutine_threadsafe") as run:
                    notion.log_push_later(settings, PAYLOAD, [_commit("a" * 40, "x")], project="Mod", loop=loop)
                run.assert_not_called()
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
