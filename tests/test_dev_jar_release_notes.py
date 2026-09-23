import asyncio
import unittest
from types import SimpleNamespace

from bulmaai.services.dev_jar_downloads import DevJarCommit
from bulmaai.services.dev_jar_release_notes import (
    build_release_notes_prompt,
    generate_dev_jar_release_notes,
)


def _commit(sha: str, title: str, description: str | None = None) -> DevJarCommit:
    return DevJarCommit(
        sha=sha,
        title=title,
        description=description,
        author="Shokkoh",
        url=f"https://github.com/DragonMineZ/dragonminez/commit/{sha}",
    )


def _fake_client(create) -> SimpleNamespace:
    return SimpleNamespace(responses=SimpleNamespace(create=create))


class BuildReleaseNotesPromptTests(unittest.TestCase):
    def test_includes_title_and_description(self) -> None:
        prompt = build_release_notes_prompt(
            (_commit("111111111111", "feat: new form", "Adds a new transformation."),)
        )
        self.assertIn("feat: new form", prompt)
        self.assertIn("Adds a new transformation.", prompt)

    def test_omits_description_when_absent(self) -> None:
        prompt = build_release_notes_prompt((_commit("111111111111", "chore: bump deps"),))
        self.assertEqual(prompt, "- chore: bump deps")

    def test_caps_number_of_commits(self) -> None:
        commits = tuple(_commit(f"{i:012x}", f"fix: commit {i}") for i in range(60))
        prompt = build_release_notes_prompt(commits)
        self.assertEqual(prompt.count("\n") + 1, 40)
        self.assertIn("fix: commit 39", prompt)
        self.assertNotIn("fix: commit 40", prompt)


class GenerateDevJarReleaseNotesTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_raw_commit_rendering_when_client_missing(self) -> None:
        commits = (_commit("222222222222", "fix: race selection screen fix"),)

        result = await generate_dev_jar_release_notes(commits, client=None, model="gpt-5-mini")

        self.assertIn("fix: race selection screen fix", result)
        self.assertIn("Shokkoh", result)

    async def test_falls_back_to_empty_string_when_no_commits(self) -> None:
        result = await generate_dev_jar_release_notes((), client=None, model="gpt-5-mini")
        self.assertEqual(result, "")

    async def test_falls_back_on_api_failure(self) -> None:
        commits = (_commit("222222222222", "fix: race selection screen fix"),)

        async def failing_create(**kwargs):
            raise RuntimeError("OpenAI is down")

        client = _fake_client(failing_create)
        result = await generate_dev_jar_release_notes(commits, client=client, model="gpt-5-mini")

        self.assertIn("fix: race selection screen fix", result)

    async def test_falls_back_on_timeout(self) -> None:
        commits = (_commit("222222222222", "fix: race selection screen fix"),)

        async def slow_create(**kwargs):
            await asyncio.sleep(0.2)
            return SimpleNamespace(output_text="too late")

        client = _fake_client(slow_create)
        result = await generate_dev_jar_release_notes(
            commits, client=client, model="gpt-5-mini", timeout_seconds=0.01
        )

        self.assertIn("fix: race selection screen fix", result)
        self.assertNotIn("too late", result)

    async def test_falls_back_on_empty_response(self) -> None:
        commits = (_commit("222222222222", "fix: race selection screen fix"),)

        async def empty_create(**kwargs):
            return SimpleNamespace(output_text="   ")

        client = _fake_client(empty_create)
        result = await generate_dev_jar_release_notes(commits, client=client, model="gpt-5-mini")

        self.assertIn("fix: race selection screen fix", result)

    async def test_returns_ai_blurb_on_success(self) -> None:
        commits = (_commit("222222222222", "fix: race selection screen fix"),)

        async def ok_create(**kwargs):
            return SimpleNamespace(output_text="  New race select screen bugfixes!  ")

        client = _fake_client(ok_create)
        result = await generate_dev_jar_release_notes(commits, client=client, model="gpt-5-mini")

        self.assertEqual(result, "New race select screen bugfixes!")

    async def test_uses_low_reasoning_effort_for_gpt5_models(self) -> None:
        commits = (_commit("222222222222", "fix: race selection screen fix"),)
        seen_kwargs: dict = {}

        async def capture_create(**kwargs):
            seen_kwargs.update(kwargs)
            return SimpleNamespace(output_text="blurb")

        client = _fake_client(capture_create)
        await generate_dev_jar_release_notes(commits, client=client, model="gpt-5-mini")

        self.assertEqual(seen_kwargs["reasoning"], {"effort": "low"})
        self.assertEqual(seen_kwargs["model"], "gpt-5-mini")


if __name__ == "__main__":
    unittest.main()
