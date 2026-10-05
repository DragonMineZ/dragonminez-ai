import tempfile
import unittest
from pathlib import Path

from bulmaai.services.update_plan import plan_update


SOURCES = {
    "bulmaai/__init__.py": "",
    "bulmaai/bot.py": "from bulmaai.services import svc\nfrom bulmaai.web import server\n",
    "bulmaai/cogs/__init__.py": "",
    "bulmaai/cogs/early.py": "from bulmaai.services.svc import helper\n",
    "bulmaai/cogs/late.py": "from bulmaai.services import svc\n",
    "bulmaai/cogs/uses_early.py": "from bulmaai.cogs.early import thing\n",
    "bulmaai/cogs/in_function.py": "def f():\n    from bulmaai.services.svc import helper\n",
    "bulmaai/cogs/self_update.py": "from bulmaai.services.svc import helper\n",
    "bulmaai/services/__init__.py": "",
    "bulmaai/services/svc.py": "def helper(): pass\n",
    "bulmaai/services/base.py": "X = 1\n",
    "bulmaai/services/mid.py": "from bulmaai.services.base import X\n",
    "bulmaai/services/stateful.py": "__hot_reload__ = False\nCACHE = {}\n",
    "bulmaai/cogs/stateful_user.py": "from bulmaai.services.stateful import CACHE\n",
    "bulmaai/cogs/mid_user.py": "from bulmaai.services.mid import X\n",
    "bulmaai/web/__init__.py": "",
    "bulmaai/web/server.py": "from bulmaai.web import routes_things\n",
    "bulmaai/web/routes_things.py": "from bulmaai.services.svc import helper\n",
}
TESTS = {
    "test_early.py": "from bulmaai.cogs.early import thing\n",
    "test_late.py": "from bulmaai.cogs import late\n",
    "test_svc.py": "from bulmaai.services import svc\n",
    "test_admin_panel.py": "from bulmaai.web.server import create_app\n",
    "test_admin_panel_things.py": "from test_admin_panel import make_bot\nfrom bulmaai.web.server import create_app\n",
    "test_unrelated.py": "import json\n",
}
EXTENSIONS = {
    "bulmaai.cogs.early",
    "bulmaai.cogs.late",
    "bulmaai.cogs.uses_early",
    "bulmaai.cogs.in_function",
    "bulmaai.cogs.self_update",
    "bulmaai.cogs.stateful_user",
    "bulmaai.cogs.mid_user",
}


class UpdatePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src, self.tests = root / "src", root / "tests"
        for files, base in ((SOURCES, self.src), (TESTS, self.tests)):
            for name, body in files.items():
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def plan(self, *paths: str):
        return plan_update(paths, src_root=self.src, tests_root=self.tests, loaded_extensions=EXTENSIONS)

    def test_docs_and_tests_only_are_noop(self) -> None:
        plan = self.plan("README.md", ".github/workflows/ci.yml", "tests/test_svc.py")
        self.assertEqual(plan.mode, "noop")

    def test_requirements_need_a_restart(self) -> None:
        plan = self.plan("requirements.txt", "src/bulmaai/cogs/late.py")
        self.assertEqual(plan.mode, "full")
        self.assertIn("requirements.txt", plan.reasons[0])

    def test_single_cog_reloads_only_itself_and_runs_only_its_tests(self) -> None:
        plan = self.plan("src/bulmaai/cogs/late.py")
        self.assertEqual(plan.mode, "hot")
        self.assertEqual(plan.extensions_to_reload, ["bulmaai.cogs.late"])
        self.assertEqual(plan.modules_to_reload, [])
        self.assertFalse(plan.rebind_panel)
        self.assertEqual(plan.tests, ["test_late"])

    def test_cog_imported_by_another_cog_reloads_both_in_order(self) -> None:
        plan = self.plan("src/bulmaai/cogs/early.py")
        self.assertEqual(plan.extensions_to_reload, ["bulmaai.cogs.early", "bulmaai.cogs.uses_early"])

    def test_service_change_reloads_only_name_importers(self) -> None:
        plan = self.plan("src/bulmaai/services/svc.py")
        self.assertEqual(plan.mode, "hot")
        # late (module import) and in_function (call-time import) see the new code without a reload.
        self.assertEqual(
            set(plan.extensions_to_reload), {"bulmaai.cogs.early", "bulmaai.cogs.uses_early", "bulmaai.cogs.self_update"}
        )
        self.assertEqual(plan.extensions_to_reload[-1], "bulmaai.cogs.self_update")
        self.assertEqual(plan.modules_to_reload, ["bulmaai.services.svc", "bulmaai.web.routes_things"])
        self.assertTrue(plan.rebind_panel)
        self.assertIn("test_svc", plan.tests)
        self.assertIn("test_late", plan.tests)  # direct importer still gets its tests run
        self.assertIn("test_admin_panel_things", plan.tests)  # named after routes_things
        self.assertNotIn("test_unrelated", plan.tests)

    def test_dependencies_reload_before_dependents(self) -> None:
        plan = self.plan("src/bulmaai/services/base.py")
        self.assertEqual(plan.modules_to_reload, ["bulmaai.services.base", "bulmaai.services.mid"])
        self.assertEqual(plan.extensions_to_reload, ["bulmaai.cogs.mid_user"])

    def test_pinned_module_and_its_name_importers_need_a_restart(self) -> None:
        self.assertEqual(self.plan("src/bulmaai/services/stateful.py").mode, "full")
        self.assertEqual(self.plan("src/bulmaai/bot.py").mode, "full")

    def test_static_files_rebind_the_panel(self) -> None:
        plan = self.plan("src/bulmaai/web/static/panel.css")
        self.assertEqual(plan.mode, "hot")
        self.assertEqual(plan.modules_to_reload, ["bulmaai.web.server"])
        self.assertTrue(plan.rebind_panel)
        self.assertNotIn("test_early", plan.tests)

    def test_prompt_text_only_clears_caches(self) -> None:
        plan = self.plan("src/bulmaai/configs/prompts/support_facts_en.txt")
        self.assertEqual(plan.mode, "hot")
        self.assertTrue(plan.clear_caches)
        self.assertEqual(plan.extensions_to_reload, [])

    def test_schema_change_reruns_schema(self) -> None:
        plan = self.plan("scripts/schema.sql")
        self.assertEqual(plan.mode, "hot")
        self.assertTrue(plan.rerun_schema)

    def test_changed_shared_fixture_runs_its_users(self) -> None:
        plan = self.plan("tests/test_admin_panel.py")
        self.assertEqual(plan.tests, ["test_admin_panel", "test_admin_panel_things"])


if __name__ == "__main__":
    unittest.main()
