import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.cogs import bug_reports as bug_cog
from bulmaai.cogs.bug_reports import BugReportsCog
from bulmaai.services import notion_tracker
from bulmaai.services.bug_report_ai import BugTriage


class ItemBodyTests(unittest.TestCase):
    def test_new_item_is_an_inbox_mod_bug_with_priority_from_severity(self) -> None:
        body = notion_tracker.new_item(
            "ds-1", title="Crash on load", summary="x" * 3000, severity="critical",
            reporter="goku (Discord)", thread_url="https://discord.com/channels/1/2",
        )
        props = body["properties"]
        self.assertEqual(body["parent"], {"type": "data_source_id", "data_source_id": "ds-1"})
        self.assertEqual(props["Status"], {"status": {"name": "Inbox"}})
        self.assertEqual(props["Type"]["select"]["name"], "Bug")
        self.assertEqual(props["Project"]["select"]["name"], "Mod")
        self.assertEqual(props["Priority"]["select"]["name"], "P0 Critical")
        self.assertEqual(len(body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]), 2000)

    def test_unknown_severity_is_normal(self) -> None:
        body = notion_tracker.new_item("d", title="", summary="", severity="weird", reporter="", thread_url="u")
        self.assertEqual(body["properties"]["Priority"]["select"]["name"], "P2 Normal")
        self.assertEqual(body["properties"]["Name"]["title"][0]["text"]["content"], "Bug report")

    def test_item_update_only_sends_what_changed(self) -> None:
        self.assertEqual(notion_tracker.item_update(), {"properties": {}})
        self.assertEqual(
            notion_tracker.item_update(status="Done", github="https://github.com/x/y/issues/1"),
            {"properties": {"Status": {"status": {"name": "Done"}}, "GitHub": {"url": "https://github.com/x/y/issues/1"}}},
        )


class CogHookTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, token: str | None = "secret"):
        cog = BugReportsCog.__new__(BugReportsCog)
        cog.bot = SimpleNamespace()
        cog.settings = SimpleNamespace(notion_token=token, notion_tracker_data_source_id="ds-1")
        cog._notion_tasks = set()
        return cog

    async def _drain(self, cog) -> None:
        await asyncio.gather(*cog._notion_tasks)

    async def test_bug_becomes_a_tracker_item_once(self) -> None:
        cog = self._cog()
        thread = MagicMock(spec=discord.Thread)
        thread.id, thread.guild = 9, SimpleNamespace(id=1, get_member=lambda _id: None)
        triage = BugTriage(is_bug=True, title="Crash", summary="NPE", severity="high", affected_area="")
        report = SimpleNamespace(notion_page_id=None)
        with patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=report)), \
                patch.object(notion_tracker, "create_item", AsyncMock(return_value="page-1")) as create, \
                patch.object(bug_cog, "set_notion_page", AsyncMock()) as store:
            cog._notion(cog._notion_create(thread, triage, 42))
            await self._drain(cog)
            report.notion_page_id = "page-1"
            cog._notion(cog._notion_create(thread, triage, 42))
            await self._drain(cog)
        create.assert_awaited_once()
        self.assertEqual(create.await_args.kwargs["reporter"], "Discord user 42")
        self.assertEqual(create.await_args.kwargs["thread_url"], "https://discord.com/channels/1/9")
        store.assert_awaited_once_with(9, "page-1")

    async def test_final_statuses_reach_notion_and_others_do_not(self) -> None:
        cog = self._cog()
        report = SimpleNamespace(notion_page_id="page-1")
        with patch.object(bug_cog, "set_status", AsyncMock()), \
                patch.object(bug_cog, "get_bug_report", AsyncMock(return_value=report)), \
                patch.object(notion_tracker, "update_item", AsyncMock()) as update:
            await cog._set_status(9, "resolved")
            await cog._set_status(9, "dismissed")
            await cog._set_status(9, "tracked")
            await self._drain(cog)
        statuses = [call.kwargs["status"] for call in update.await_args_list]
        self.assertEqual(sorted(statuses), ["Done", "Won't do"])

    async def test_nothing_runs_without_a_token(self) -> None:
        cog = self._cog(token=None)
        with patch.object(bug_cog, "set_status", AsyncMock()) as set_status, \
                patch.object(notion_tracker, "update_item", AsyncMock()) as update:
            await cog._set_status(9, "resolved")
        set_status.assert_awaited_once_with(9, "resolved")
        self.assertEqual(cog._notion_tasks, set())
        update.assert_not_awaited()

    async def test_notion_failure_never_raises(self) -> None:
        cog = self._cog()
        with patch.object(bug_cog, "get_bug_report", AsyncMock(side_effect=RuntimeError("db down"))):
            await cog._notion_update(9, status="Done")


if __name__ == "__main__":
    unittest.main()
