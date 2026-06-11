import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bulmaai.cogs.patch_notes_updates import (
    PatchNotesUpdatesCog,
    build_patch_notes_update_embed,
)
from bulmaai.services.patch_notes import (
    PATCH_NOTES_BRANCH,
    PATCH_NOTES_FILE_PATH,
    PATCH_NOTES_URL,
    PatchNotesState,
    summarize_patch_notes_update,
)


class PatchNotesSummaryTests(unittest.TestCase):
    def test_summary_lists_added_lines_only(self) -> None:
        old = "# Patch Notes\n- Fixed ki blasts\n"
        new = "# Patch Notes\n- Fixed ki blasts\n- Added fusion dance\n- Nerfed senzu beans\n"

        summary = summarize_patch_notes_update(old, new)

        self.assertIn("- Added fusion dance", summary)
        self.assertIn("- Nerfed senzu beans", summary)
        self.assertNotIn("Fixed ki blasts", summary)

    def test_summary_truncates_long_updates(self) -> None:
        old = ""
        new = "\n".join(f"- Change number {index}" for index in range(40))

        summary = summarize_patch_notes_update(old, new, max_lines=5)

        self.assertIn("- Change number 0", summary)
        self.assertIn("more new line", summary)
        self.assertLessEqual(len(summary), 1024)

    def test_summary_handles_removed_only_changes(self) -> None:
        old = "- Old line\n- Another\n"
        new = "- Old line\n"

        summary = summarize_patch_notes_update(old, new)

        self.assertIn("revised", summary)


class PatchNotesEmbedTests(unittest.TestCase):
    def test_embed_links_patch_notes_and_mentions_day(self) -> None:
        updated_at = datetime(2026, 6, 10, 9, 5, tzinfo=timezone.utc)

        embed = build_patch_notes_update_embed(summary="- Added fusion dance", updated_at=updated_at)

        self.assertEqual(embed.url, PATCH_NOTES_URL)
        self.assertIn("June 10, 2026", embed.description)
        self.assertIn("9 AM", embed.description)
        field_values = {field.name: field.value for field in embed.fields}
        self.assertEqual(field_values["What's new"], "- Added fusion dance")


class PatchNotesPollTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, *, file_content: str) -> PatchNotesUpdatesCog:
        cog = PatchNotesUpdatesCog.__new__(PatchNotesUpdatesCog)
        cog.bot = SimpleNamespace()
        cog.gh = SimpleNamespace(get_file=AsyncMock(return_value=(file_content, "blobsha")))
        cog._announced: list[str] = []

        async def fake_announce(summary: str) -> None:
            cog._announced.append(summary)

        cog._announce_update = fake_announce
        return cog

    async def test_first_poll_seeds_state_without_announcing(self) -> None:
        cog = self._cog(file_content="# Patch Notes\n- First entry\n")
        stored: list[PatchNotesState] = []

        with (
            patch(
                "bulmaai.cogs.patch_notes_updates.get_patch_notes_state",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "bulmaai.cogs.patch_notes_updates.upsert_patch_notes_state",
                new=AsyncMock(side_effect=stored.append),
            ),
        ):
            await cog._poll_once()

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].branch, PATCH_NOTES_BRANCH)
        self.assertEqual(stored[0].file_path, PATCH_NOTES_FILE_PATH)
        self.assertEqual(cog._announced, [])

    async def test_changed_content_announces_whats_new(self) -> None:
        old_content = "# Patch Notes\n- First entry\n"
        new_content = "# Patch Notes\n- First entry\n- Added fusion dance\n"
        cog = self._cog(file_content=new_content)
        previous = PatchNotesState(
            branch=PATCH_NOTES_BRANCH,
            file_path=PATCH_NOTES_FILE_PATH,
            content_sha="old-sha",
            content=old_content,
        )

        with (
            patch(
                "bulmaai.cogs.patch_notes_updates.get_patch_notes_state",
                new=AsyncMock(return_value=previous),
            ),
            patch(
                "bulmaai.cogs.patch_notes_updates.upsert_patch_notes_state",
                new=AsyncMock(),
            ),
        ):
            await cog._poll_once()

        self.assertEqual(len(cog._announced), 1)
        self.assertIn("- Added fusion dance", cog._announced[0])

    async def test_unchanged_content_does_nothing(self) -> None:
        content = "# Patch Notes\n- First entry\n"
        cog = self._cog(file_content=content)
        import hashlib

        previous = PatchNotesState(
            branch=PATCH_NOTES_BRANCH,
            file_path=PATCH_NOTES_FILE_PATH,
            content_sha=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            content=content,
        )
        upsert = AsyncMock()

        with (
            patch(
                "bulmaai.cogs.patch_notes_updates.get_patch_notes_state",
                new=AsyncMock(return_value=previous),
            ),
            patch(
                "bulmaai.cogs.patch_notes_updates.upsert_patch_notes_state",
                new=upsert,
            ),
        ):
            await cog._poll_once()

        upsert.assert_not_awaited()
        self.assertEqual(cog._announced, [])


if __name__ == "__main__":
    unittest.main()
