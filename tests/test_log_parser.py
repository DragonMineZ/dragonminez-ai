import time
import unittest

from bulmaai.utils.dmzdebug_parser import _RE_MEMBER, _parse_pending_invite
from bulmaai.utils.log_parser import is_noise, parse_log


class ParserBacktrackingTests(unittest.TestCase):
    """Uploaded logs are member input parsed on the bot's event loop; a quadratic regex froze the whole bot."""

    def test_hostile_logs_parse_in_linear_time(self) -> None:
        spaces = " " * 200_000
        started = time.perf_counter()
        parse_log("minecraft\n" + ("x\n" + spaces + "\n") * 3)
        parse_log(("[12:00:00] [main/INFO]: minecraft loading assets\n") * 40_000)
        _RE_MEMBER.match("0a (x)" + spaces + "z")
        _parse_pending_invite("from x" + spaces + "y")
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_mod_table_and_pending_invite_still_parse(self) -> None:
        report = parse_log("minecraft\n| xenon.jar | Xenon | xenon | 0.3.31 | DONE |\n")
        self.assertIsNotNone(report)
        self.assertEqual(
            _parse_pending_invite("from Goku (bar) (0a1b-2c)"), {"name": "Goku (bar)", "uuid": "0a1b-2c", "expired": False}
        )

    def test_hostile_error_lines_parse_in_linear_time(self) -> None:
        started = time.perf_counter()
        parse_log("[12:00:00] [main/ERROR]: " + "a." * 100_000 + "\n" + "[" * 200_000 + "\n")
        parse_log(("[12:00:00] [main/ERROR]: boom\n\tat x.Y.z(Y.java:1)\n") * 20_000)
        self.assertLess(time.perf_counter() - started, 3.0)


# Trimmed from a real Forge 47.4.10 / MC 1.20.1 latest.log that produced a bad card.
_NOISY_LOG = """\
[19:07:30] [main/INFO]: ModLauncher running: args [--fml.forgeVersion, 47.4.10, --fml.mcVersion, 1.20.1]
[19:07:31] [main/INFO]: ModLauncher 10.0.9+10.0.9+main.dcd20f30 starting: java version 17.0.8 by Microsoft; OS Windows 11 arch amd64 version 10.0
[19:07:35] [main/ERROR]: Mod mixin into Embeddium internals detected. This instance is now tainted. The Embeddium team does not provide any guarantee of support.
[19:07:39] [main/WARN]: Error loading class: blusunrize/immersiveengineering/api/wires/GlobalWireNetwork (java.lang.ClassNotFoundException: blusunrize.immersiveengineering.api.wires.GlobalWireNetwork)
[19:07:39] [main/WARN]: @Mixin target blusunrize.immersiveengineering.api.wires.GlobalWireNetwork was not found mixins.epp.json:GlobalWireNetworkMixin
[19:07:40] [main/INFO]: Loaded config for: betterfpsdist.json
[19:07:41] [main/WARN]: Force-disabling mixin 'features.render.entity.CuboidMixin' as rule 'mixin.features.render.entity' (added by mods [oculus]) disables it
[19:07:50] [modloading-worker-0/ERROR]: A mod is calling registerReloadListener at the wrong time. This will cause random concurrency crashes when ModernFix is installed.
[19:07:52] [Forge Version Check/ERROR]: io exception while checking versions: Online mod data expired! Date: Wed Oct 07 07:36:48 BRT 2026
[19:08:01] [Render thread/INFO]: Connecting to dragonminez 22mkrei.enxada.gg, 25565
"""


class LogParserTests(unittest.TestCase):
    def test_known_noise_is_not_an_error(self) -> None:
        report = parse_log(_NOISY_LOG)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.error_line_numbers, [])
        self.assertIsNone(report.stacktrace)
        self.assertTrue(is_noise("[x] [main/ERROR]: Failed to verify authentication"))
        self.assertFalse(is_noise("[x] [main/ERROR]: Failed to load dragonminez config"))

    def test_versions_still_parse(self) -> None:
        report = parse_log(_NOISY_LOG)
        self.assertEqual((report.java_version, report.forge_version, report.mc_version), ("17.0.8", "47.4.10", "1.20.1"))
        self.assertTrue(report.is_forge)

    def test_dragonminez_version_needs_a_real_version(self) -> None:
        self.assertIsNone(parse_log(_NOISY_LOG).dragonminez_version)
        self.assertEqual(parse_log("Loading mods/dragonminez-2.1.3.jar\n").dragonminez_version, "2.1.3")
        self.assertEqual(parse_log("DragonMineZ v2.2.0-beta loaded\n").dragonminez_version, "2.2.0-beta")
        self.assertEqual(parse_log("DragonMineZ version: 2.1.3\n").dragonminez_version, "2.1.3")

    def test_trace_stops_at_next_log_line(self) -> None:
        log = (
            "[19:07:35] [Render thread/ERROR]: Failed to load something\n"
            "java.lang.IllegalStateException: broken\n"
            "\tat com.example.Foo.bar(Foo.java:10)\n"
            "Caused by: java.lang.NullPointerException: null\n"
            "\t... 4 more\n"
            "[19:07:39] [main/WARN]: Error loading class: a/b/C (java.lang.ClassNotFoundException: a.b.C)\n"
            "[19:07:40] [main/INFO]: Loaded config for: betterfpsdist.json\n"
        )
        report = parse_log(log)
        self.assertEqual(
            report.stacktrace,
            "[19:07:35] [Render thread/ERROR]: Failed to load something\n"
            "java.lang.IllegalStateException: broken\n"
            "at com.example.Foo.bar(Foo.java:10)\n"
            "Caused by: java.lang.NullPointerException: null\n"
            "... 4 more",
        )

    def test_error_without_trace_is_not_a_stacktrace(self) -> None:
        report = parse_log("[19:07:35] [main/ERROR]: Something failed\n[19:07:36] [main/INFO]: Loaded config for: x.json\n")
        self.assertEqual(len(report.errors), 1)
        self.assertIsNone(report.stacktrace)

    def test_noise_trace_is_skipped_and_dragonminez_trace_preferred(self) -> None:
        log = (
            "[10:00:00] [Forge Version Check/ERROR]: io exception while checking versions: offline\n"
            "java.io.IOException: offline\n"
            "\tat net.minecraftforge.fml.VersionChecker$1.run(VersionChecker.java:1)\n"
            "[10:00:01] [Render thread/ERROR]: Other mod failed\n"
            "java.lang.RuntimeException: other\n"
            "\tat com.other.Mod.init(Mod.java:1)\n"
            "[10:00:02] [Server thread/WARN]: Ticking skill failed\n"
            "java.lang.NullPointerException: null\n"
            "\tat com.dragonminez.common.MovementSkillsHandler.tick(MovementSkillsHandler.java:42)\n"
        )
        report = parse_log(log)
        self.assertEqual(report.errors, ["[10:00:01] [Render thread/ERROR]: Other mod failed"])
        self.assertIn("MovementSkillsHandler", report.stacktrace)
        self.assertNotIn("VersionChecker", report.stacktrace)
        no_dmz = parse_log(log.split("[10:00:02]")[0])
        self.assertIn("com.other.Mod.init", no_dmz.stacktrace)

    def test_crash_report_description_comes_first(self) -> None:
        log = (
            "---- Minecraft Crash Report ----\n"
            "// Who set us up the TNT?\n"
            "\n"
            "Description: Ticking entity\n"
            "\n"
            "java.lang.NullPointerException: Cannot invoke \"Object.toString()\" because \"x\" is null\n"
            "\tat com.dragonminez.Foo.tick(Foo.java:1)\n"
            "\n"
            "A detailed walkthrough of the error, its code path and all known details is as follows:\n"
            "-- System Details --\n"
            "\tMinecraft Version: 1.20.1 forge\n"
        )
        report = parse_log(log)
        self.assertEqual(report.errors, ["Description: Ticking entity"])
        self.assertEqual(report.error_line_numbers, [4])
        self.assertTrue(report.stacktrace.startswith("Description: Ticking entity\njava.lang.NullPointerException"))
        self.assertNotIn("walkthrough", report.stacktrace)

    def test_errors_dedupe_by_message_and_keep_line_numbers(self) -> None:
        log = (
            "[10:00:00] [main/INFO]: starting\r\n"
            "[10:00:01] [Render thread/ERROR] [some.Logger/]: Same failure\r\n"
            "[10:00:02] [main/ERROR]: Mod mixin into Embeddium internals detected.\r\n"
            "[10:00:03] [Render thread/ERROR] [some.Logger/]: Same failure\r\n"
            "[10:00:04] [Server thread/FATAL]: Different failure\r\n"
        ) + "[10:00:05] [Render thread/ERROR] [some.Logger/]: Same failure\n" * 50
        report = parse_log(log)
        self.assertEqual(
            report.errors,
            [
                "[10:00:01] [Render thread/ERROR] [some.Logger/]: Same failure",
                "[10:00:04] [Server thread/FATAL]: Different failure",
            ],
        )
        self.assertEqual(report.error_line_numbers, [2, 5])

    def test_errors_are_capped(self) -> None:
        report = parse_log("".join(f"[10:00:00] [main/ERROR]: failure {i}\n" for i in range(40)))
        self.assertEqual(len(report.errors), 15)
        self.assertEqual(report.error_line_numbers, list(range(1, 16)))
