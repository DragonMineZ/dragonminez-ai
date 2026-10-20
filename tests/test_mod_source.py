import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from requests import HTTPError

from bulmaai.services import ai_tools, mod_source
from bulmaai.services.mod_source import DEFAULTS_DIR, JAVA_DIR, LANG_FILE, QUESTS_DIR, normalize
from bulmaai.utils import tools_registry

CONFIG = JAVA_DIR + "common/config/"
COMMANDS = JAVA_DIR + "server/commands/"
TREE = [
    CONFIG + "CombatConfig.java",
    CONFIG + "GeneralServerConfig.java",
    CONFIG + "migration/RacialConfigMigrator.java",
    COMMANDS + "DMZPermissions.java",
    COMMANDS + "StatsCommand.java",
    JAVA_DIR + "server/DMZServer.java",
    JAVA_DIR + "server/storage/PlayerStorage.java",
    DEFAULTS_DIR + "general-server.json",
    DEFAULTS_DIR + "races/saiyan/stats.json",
    DEFAULTS_DIR + "races/saiyan/forms/supersaiyan.json",
    LANG_FILE,
    "src/main/resources/assets/dragonminez/lang/es_es.json",
    QUESTS_DIR + "sagas/buu_saga.json",
    QUESTS_DIR + "quests/saga_buu/05_tournament_goten.json",
    "build.gradle.kts",
    "src/main/resources/assets/dragonminez/textures/item/scouter.png",
]


def settings(branch: str = "v2.2") -> SimpleNamespace:
    return SimpleNamespace(
        mod_source_branch=branch,
        GH_APP_ID="1",
        GH_INSTALLATION_ID="2",
        GH_APP_PRIVATE_KEY_PEM="key",
        GITHUB_OWNER="DragonMineZ",
        GITHUB_DEFAULT_REPO="dragonminez",
        GITHUB_BASE_BRANCH="main",
    )


def not_found() -> HTTPError:
    return HTTPError("404", response=SimpleNamespace(status_code=404))


class GitHubTestCase(unittest.IsolatedAsyncioTestCase):
    """GitHubService is replaced by a mock class; tree and files come from TREE / self.files."""

    def setUp(self) -> None:
        mod_source.clear_cache()
        self.addCleanup(mod_source.clear_cache)
        self.files: dict[str, str] = {}
        self.service = MagicMock()
        self.service.list_tree = AsyncMock(return_value=list(TREE))
        self.service.get_file = AsyncMock(side_effect=self._get_file)
        service_patch = patch.object(mod_source, "GitHubService", return_value=self.service)
        self.service_cls = service_patch.start()
        self.addCleanup(service_patch.stop)
        auth_patch = patch.object(mod_source, "GitHubAppAuth")
        auth_patch.start()
        self.addCleanup(auth_patch.stop)

    async def _get_file(self, path: str, ref: str):
        if path not in self.files:
            raise not_found()
        return self.files[path], "sha"


class AllowlistTests(unittest.TestCase):
    def test_allowlisted_paths_and_their_areas(self) -> None:
        for path, area in (
            (CONFIG + "GeneralServerConfig.java", "config"),
            (COMMANDS + "DMZPermissions.java", "commands"),
            (JAVA_DIR + "server/DMZServer.java", "commands"),
            (DEFAULTS_DIR + "races/saiyan/forms/supersaiyan.json", "defaults"),
            (LANG_FILE, "lang"),
            (QUESTS_DIR + "quests/saga_buu/05_tournament_goten.json", "quests"),
        ):
            with self.subTest(path=path):
                self.assertEqual(normalize(path), path)
                self.assertEqual(mod_source.area_of(path), area)

    def test_traversal_absolute_and_odd_paths_are_refused(self) -> None:
        for path in (
            DEFAULTS_DIR + "../../../../../../build.gradle.kts",
            DEFAULTS_DIR + "races/../general-server.json",
            DEFAULTS_DIR + "./general-server.json",
            DEFAULTS_DIR + "races//saiyan/stats.json",
            "/" + LANG_FILE,
            "C:/" + LANG_FILE,
            DEFAULTS_DIR + "%2e%2e/%2e%2e/secrets.json",
            LANG_FILE + "?ref=main",
            "",
            None,
        ):
            with self.subTest(path=path):
                self.assertIsNone(normalize(path))

    def test_paths_outside_the_allowlist_are_refused(self) -> None:
        for path in (
            "build.gradle.kts",
            CONFIG + "migration/RacialConfigMigrator.java",  # config is one level only
            COMMANDS + "sub/Thing.java",
            JAVA_DIR + "server/storage/PlayerStorage.java",
            "src/main/resources/assets/dragonminez/lang/es_es.json",
            DEFAULTS_DIR + "notes.txt",
            DEFAULTS_DIR.rstrip("/"),
            CONFIG,
        ):
            with self.subTest(path=path):
                self.assertIsNone(normalize(path))

    def test_backslashes_and_runtime_config_paths_are_normalized(self) -> None:
        self.assertEqual(normalize(LANG_FILE.replace("/", "\\")), LANG_FILE)
        self.assertEqual(normalize("  config/dragonminez/general-server.json "), DEFAULTS_DIR + "general-server.json")
        self.assertEqual(
            normalize("config/dragonminez/races/saiyan/stats.json"), DEFAULTS_DIR + "races/saiyan/stats.json"
        )
        self.assertIsNone(normalize("config/dragonminez/../../options.txt"))


class ListTests(GitHubTestCase):
    async def test_each_area_lists_only_its_allowlisted_files(self) -> None:
        expected = {
            "config": [CONFIG + "CombatConfig.java", CONFIG + "GeneralServerConfig.java"],
            "commands": [JAVA_DIR + "server/DMZServer.java", COMMANDS + "DMZPermissions.java", COMMANDS + "StatsCommand.java"],
            "defaults": [
                DEFAULTS_DIR + "general-server.json",
                DEFAULTS_DIR + "races/saiyan/forms/supersaiyan.json",
                DEFAULTS_DIR + "races/saiyan/stats.json",
            ],
            "lang": [LANG_FILE],
            "quests": [QUESTS_DIR + "quests/saga_buu/05_tournament_goten.json", QUESTS_DIR + "sagas/buu_saga.json"],
        }
        for area, files in expected.items():
            with self.subTest(area=area):
                result = await mod_source.list_files(settings(), area)
                self.assertEqual(result["files"], files)
                self.assertEqual((result["area"], result["branch"], result["total"]), (area, "v2.2", len(files)))
                self.assertFalse(result["truncated"])
                self.assertNotIn("folders", result)

    async def test_one_tree_call_on_the_branch_setting_is_cached(self) -> None:
        await mod_source.list_files(settings("main"), "config")
        await mod_source.list_files(settings("main"), "lang")
        self.service.list_tree.assert_awaited_once_with("main")
        kwargs = self.service_cls.call_args.kwargs
        self.assertEqual((kwargs["owner"], kwargs["repo"]), ("DragonMineZ", "dragonminez"))
        await mod_source.list_files(settings("v2.2"), "config")  # another branch is another cache entry
        self.assertEqual(self.service.list_tree.await_count, 2)

    async def test_long_listings_are_capped_with_folder_counts_and_a_folder_narrows_them(self) -> None:
        quests = [f"{QUESTS_DIR}quests/saga_buu/{i:03d}.json" for i in range(70)]
        quests += [f"{QUESTS_DIR}sidequests/combat/{i:03d}.json" for i in range(20)]
        self.service.list_tree.return_value = quests
        result = await mod_source.list_files(settings(), "quests")
        self.assertEqual((len(result["files"]), result["total"], result["truncated"]), (mod_source.MAX_LISTED, 90, True))
        self.assertEqual(
            result["folders"], {f"{QUESTS_DIR}quests/saga_buu": 70, f"{QUESTS_DIR}sidequests/combat": 20}
        )
        narrowed = await mod_source.list_files(settings(), "quests", f"{QUESTS_DIR}sidequests/combat/")
        self.assertEqual((narrowed["total"], narrowed["truncated"]), (20, False))
        self.assertTrue(all("/sidequests/combat/" in path for path in narrowed["files"]))

    async def test_errors(self) -> None:
        self.assertIn("Unknown area", (await mod_source.list_files(settings(), "assets"))["error"])
        self.assertIn("No quests files", (await mod_source.list_files(settings(), "quests", "src/nowhere"))["error"])
        self.assertIn("Unknown folder", (await mod_source.list_files(settings(), "quests", "../x?y"))["error"])
        mod_source.clear_cache()
        self.service.list_tree.side_effect = RuntimeError("boom")
        with self.assertLogs(mod_source.log, level="ERROR"):
            result = await mod_source.list_files(settings(), "config")
        self.assertEqual(result, {"error": mod_source.GITHUB_FAILED})


class ReadTests(GitHubTestCase):
    async def test_window_from_the_top_and_from_start_line(self) -> None:
        self.files[LANG_FILE] = "{\n  \"a\": \"A\",\n  \"b\": \"B\"\n}\n"
        result = await mod_source.read_file(settings(), LANG_FILE)
        self.assertEqual(
            result,
            {
                "path": LANG_FILE,
                "branch": "v2.2",
                "lines": '1: {\n2:   "a": "A",\n3:   "b": "B"\n4: }',
                "total_lines": 4,
                "truncated": False,
            },
        )
        self.service.get_file.assert_awaited_once_with(LANG_FILE, ref="v2.2")
        later = await mod_source.read_file(settings(), LANG_FILE, start_line=3)
        self.assertEqual(later["lines"], '3:   "b": "B"\n4: }')
        self.assertEqual((await mod_source.read_file(settings(), LANG_FILE, start_line=0))["lines"].split("\n")[0], "1: {")
        self.assertIn("past the end", (await mod_source.read_file(settings(), LANG_FILE, start_line=9))["error"])
        self.service.get_file.assert_awaited_once()  # every read after the first came from the cache

    async def test_window_is_capped_and_long_lines_are_cut(self) -> None:
        path = CONFIG + "CombatConfig.java"
        self.files[path] = "\n".join(f"\tprivate int option{i} = {i};" for i in range(2000)) + "\n" + "x" * 1000
        result = await mod_source.read_file(settings(), path)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["lines"]), mod_source.MAX_CHARS)
        self.assertGreater(len(result["lines"]), mod_source.MAX_CHARS - 100)
        last = await mod_source.read_file(settings(), path, start_line=2001)
        self.assertFalse(last["truncated"])
        self.assertEqual(last["lines"], "2001: " + "x" * mod_source.MAX_LINE_CHARS + "…")

    async def test_query_shows_matches_with_merged_context(self) -> None:
        path = DEFAULTS_DIR + "general-server.json"
        lines = [f"line {i}" for i in range(1, 41)]
        lines[4] = '  "reviveCooldownSeconds": 300,'
        lines[6] = '  "REVIVEhealth": 1,'
        lines[29] = '  "reviveAnywhere": false,'
        self.files[path] = "\n".join(lines)
        result = await mod_source.read_file(settings(), "config/dragonminez/general-server.json", query=" revive")
        self.assertEqual((result["path"], result["query"], result["matches"], result["truncated"]), (path, "revive", 3, False))
        shown = result["lines"].split("\n")
        # lines 5 and 7 share one stretch (2-10), line 30 gets its own (27-33)
        self.assertEqual(shown[0], "2: line 2")
        self.assertEqual(shown[shown.index("...") - 1], "10: line 10")
        self.assertEqual(shown[shown.index("...") + 1], "27: line 27")
        self.assertEqual(shown[-1], "33: line 33")
        self.assertEqual(shown.count("..."), 1)
        none = await mod_source.read_file(settings(), path, query="tailWhip")
        self.assertEqual((none["matches"], none["lines"], none["truncated"]), (0, "", False))

    async def test_query_output_is_capped(self) -> None:
        path = LANG_FILE
        self.files[path] = "\n".join(f'  "dmz.key.{i}": "Ki blast number {i}",' for i in range(3000))
        result = await mod_source.read_file(settings(), path, query="ki blast")
        self.assertEqual(result["matches"], 3000)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["lines"]), mod_source.MAX_CHARS)

    async def test_refusals_and_errors(self) -> None:
        refused = await mod_source.read_file(settings(), "src/main/resources/../../../.env")
        self.assertEqual(refused, {"error": mod_source.NOT_ALLOWED})
        self.assertEqual(await mod_source.read_file(settings(), "build.gradle.kts"), {"error": mod_source.NOT_ALLOWED})
        too_long = await mod_source.read_file(settings(), LANG_FILE, query="x" * (mod_source.QUERY_MAX + 1))
        self.assertIn("too long", too_long["error"])
        self.service.get_file.assert_not_awaited()  # nothing refused ever reaches GitHub

        missing = await mod_source.read_file(settings(), CONFIG + "NopeConfig.java")
        self.assertIn("doesn't exist on v2.2", missing["error"])
        self.service.get_file.side_effect = HTTPError("500", response=SimpleNamespace(status_code=500))
        with self.assertLogs(mod_source.log, level="ERROR"):
            failed = await mod_source.read_file(settings(), CONFIG + "OtherConfig.java")
        self.assertEqual(failed, {"error": mod_source.GITHUB_FAILED})

    async def test_cache_expires(self) -> None:
        self.files[LANG_FILE] = "{}"
        await mod_source.read_file(settings(), LANG_FILE)
        stamp, text = mod_source._cache[("v2.2", LANG_FILE)]
        mod_source._cache[("v2.2", LANG_FILE)] = (stamp - mod_source.CACHE_SECONDS - 1, text)
        await mod_source.read_file(settings(), LANG_FILE)
        self.assertEqual(self.service.get_file.await_count, 2)


class ToolTests(GitHubTestCase):
    def test_schemas_are_strict_lookups(self) -> None:
        for name in ("list_mod_files", "read_mod_file"):
            with self.subTest(name=name):
                self.assertIn(name, ai_tools.LOOKUP_TOOL_NAMES)
                self.assertNotIn(name, ai_tools.IDENTITY_TOOLS)
                schema = tools_registry.get_schemas([name])[0]
                self.assertTrue(schema["strict"])
                self.assertEqual(schema["input_schema"]["required"], list(schema["input_schema"]["properties"]))
        listing = ai_tools.TOOL_SCHEMAS["list_mod_files"]
        self.assertEqual(listing["parameters"]["properties"]["area"]["enum"], ["config", "defaults", "commands", "lang", "quests"])
        self.assertIn("config/dragonminez/<file>.json", listing["description"])
        self.assertIn("DMZPermissions", listing["description"])
        self.assertIn("never paste code", ai_tools.TOOL_SCHEMAS["read_mod_file"]["description"])

    async def test_tools_use_the_bot_settings(self) -> None:
        bot = SimpleNamespace(settings=settings("dev"))
        self.files[LANG_FILE] = '{"a": "b"}'
        listed = await tools_registry.get_func("list_mod_files", bot_context=bot)(area="lang", folder=None)
        self.assertEqual((listed["branch"], listed["files"]), ("dev", [LANG_FILE]))
        read = await tools_registry.get_func("read_mod_file", bot_context=bot)(path=LANG_FILE, query=None, start_line=None)
        self.assertEqual(read["lines"], '1: {"a": "b"}')
        self.service.get_file.assert_awaited_once_with(LANG_FILE, ref="dev")


if __name__ == "__main__":
    unittest.main()
