import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "log_corpus_eval", Path(__file__).resolve().parents[1] / "scripts" / "log_corpus_eval.py"
)
log_corpus_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(log_corpus_eval)

_LOG = (
    "[12:00:00] [main/INFO]: ModLauncher running: args [--fml.forgeVersion, 47.4.10, --fml.mcVersion, 1.20.1]\n"
    "[12:00:05] [Render thread/ERROR]: Crash\n"
    "java.lang.RuntimeException: boom\n"
    "Caused by: java.lang.NoSuchMethodError: 'double com.dragonminez.common.config.CombatConfig.getDefenseReductionScale()'\n"
    "\tat com.example.addon.Foo.bar(Foo.java:1) ~[dmzcompanions-2.0.5.jar%2312!/:2.0.5]\n"
)


class EvalTests(unittest.TestCase):
    def test_table_and_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2.2.1").mkdir()
            (root / "README.txt").write_text("skip me", encoding="utf-8")
            (root / "2.2.1" / "1-latest.log").write_text(_LOG, encoding="utf-8")
            (root / "2.2.1" / "1-latest.log.json").write_text(
                json.dumps({"root_cause": None, "after": [{"who": "staff", "text": "x"}, {"who": "user", "text": "y"}]}),
                encoding="utf-8",
            )
            report = log_corpus_eval.evaluate(root)
        self.assertIn("| 2.2.1/1-latest.log |", report)
        self.assertIn("- logs: 1", report)
        self.assertIn("- root cause: 1", report)
        self.assertIn("- hints: 1", report)
        self.assertIn("- nothing found: 0", report)
        self.assertIn("| 1 |", report.splitlines()[2])
        self.assertIn("## Unexplained\n- none", report)


if __name__ == "__main__":
    unittest.main()
