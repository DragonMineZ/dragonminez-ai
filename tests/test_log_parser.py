import time
import unittest
from pathlib import Path

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


_FIXTURES = Path(__file__).parent / "fixtures" / "logs"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class RootCauseTests(unittest.TestCase):
    def test_mixin_crash_names_the_mod_and_its_jar(self) -> None:
        cause = parse_log(_fixture("noea_mixin_crash.log")).root_cause
        self.assertEqual(cause.culprit_mod, "noeabosses")
        self.assertEqual(cause.culprit_jar, "Noea-1.2.0-DMZ-2.1.3.jar")
        self.assertEqual(cause.kind, "mixin")
        self.assertTrue(cause.exception.startswith("InjectionError: Critical injection failure: Constant modifier method"))
        self.assertLessEqual(len(cause.exception), 300)
        self.assertEqual(cause.chain[0], "MixinTransformerError: An unexpected critical error was encountered")
        self.assertTrue(cause.chain[-1].startswith("InjectionError:"))
        self.assertEqual(len(cause.chain), 2)

    def test_fixture_noise_is_dropped(self) -> None:
        errors = "\n".join(parse_log(_fixture("noea_mixin_crash.log")).errors)
        self.assertNotIn("Signature is missing from Property textures", errors)
        self.assertNotIn("Embeddium internals", errors)
        self.assertTrue(is_noise("[x] [Worker-Main-1/ERROR]: Signature is missing from Property textures"))

    def test_platform_only_trace_has_no_culprit(self) -> None:
        report = parse_log(
            "[10:00:00] [Render thread/ERROR]: Failed to load something\n"
            "java.lang.IllegalStateException: broken\n"
            "\tat net.minecraft.client.Minecraft.tick(Minecraft.java:1) ~[client-1.20.1-srg.jar%23295!/:?]\n"
            "\tat java.lang.Thread.run(Unknown Source) ~[?:?]\n"
            "Caused by: java.lang.NullPointerException: null\n"
            "\tat net.minecraftforge.eventbus.EventBus.post(EventBus.java:315) ~[eventbus-6.0.5.jar%2387!/:?]\n"
            "\t... 2 more\n"
            "\t\tforge-47.4.10.jar |Forge |forge |47.4.10 |DONE |Manifest: NOSIGNATURE\n"
        )
        cause = report.root_cause
        self.assertEqual(cause.chain, ("IllegalStateException: broken", "NullPointerException: null"))
        self.assertEqual(cause.exception, "NullPointerException: null")
        self.assertIsNone(cause.culprit_mod)
        self.assertIsNone(cause.culprit_jar)
        self.assertEqual(cause.kind, "exception")

    def test_first_mod_frame_of_the_deepest_cause_is_the_culprit(self) -> None:
        report = parse_log(
            "[10:00:00] [Server thread/ERROR]: Encountered an unexpected exception\n"
            "net.minecraft.ReportedException: Ticking entity\n"
            "\tat com.other.Thing.run(Thing.java:1) ~[other-1.0.jar%2310!/:1.0]\n"
            "Caused by: java.lang.NullPointerException: boom\n"
            "\tat java.util.Objects.requireNonNull(Unknown Source) ~[?:?]\n"
            "\tat com.dragonminez.common.Foo.tick(Foo.java:4) ~[dragonminez-2.1.3.jar%23300!/:2.1.3]\n"
            "\t... 3 more\n"
            "\tMod List: \n"
            "\t\tdragonminez-2.1.3.jar |DragonMineZ |dragonminez |2.1.3 |DONE |Manifest: NOSIGNATURE\n"
            "\t\tother-1.0.jar |Other |other |1.0 |DONE |Manifest: NOSIGNATURE\n"
        )
        cause = report.root_cause
        self.assertEqual((cause.culprit_mod, cause.culprit_jar), ("dragonminez", "dragonminez-2.1.3.jar"))
        self.assertEqual(cause.kind, "exception")

    def test_no_trace_means_no_root_cause(self) -> None:
        self.assertIsNone(parse_log("[19:07:35] [main/ERROR]: Something failed\n").root_cause)


def _mixin_log(cause: str, *frames: str) -> str:
    """A latest.log mixin crash without any mod list, so the culprit can only come from the message."""
    return (
        "[10:00:00] [Render thread/ERROR]: Error loading class: x/Y\n"
        "org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: An unexpected critical error was "
        "encountered\n"
        "\tat org.spongepowered.asm.mixin.transformer.MixinProcessor.applyMixins(MixinProcessor.java:392) "
        "~[mixin-0.8.5.jar:0.8.5]\n"
        f"Caused by: {cause}\n"
        + "".join(f"\tat {frame}\n" for frame in frames)
        + "\t... 12 more\n"
    )


class ModListTests(unittest.TestCase):
    def test_loading_mods_block_is_read(self) -> None:
        report = parse_log(
            "[15:14:58] [main/INFO]: Found valid mod file appleskin.jar with {appleskin} mods - versions {9.9}\n"
            "[15:14:59] [main/INFO]: Loading 4 mods:\n"
            "\t- ad_astra 1.15.21\n"
            "\t- appleskin 2.5.1+mc1.20.1\n"
            "\t- Create 6.0.8\n"
            "\t\t|-- flywheel 1.0.5\n"
            "\t\t\\-- ponder 1.0.91\n"
            "[15:15:00] [main/INFO]: Launching target 'forgeclient'\n"
            "\t- notamod 1.0\n"
        )
        self.assertEqual(
            report.mods,
            {"appleskin": "9.9", "ad_astra": "1.15.21", "create": "6.0.8", "flywheel": "1.0.5", "ponder": "1.0.91"},
        )

    def test_loading_mods_block_parses_in_linear_time(self) -> None:
        started = time.perf_counter()
        parse_log("Loading 1 mods:\n" + ("\t" * 200_000 + "x\n") * 3)
        parse_log("Loading 1 mods:\n" + "\t- a 1\n" * 100_000)
        parse_log("Found mod file " + "a" * 200_000 + "\n")
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_found_mod_files_become_jars(self) -> None:
        provider = " with provider {mods folder locator at C:\\mods}\n"
        report = parse_log(
            f"[20:27:55] [main/INFO]: Found mod file appleskin-forge-mc1.20.1-2.5.1.jar of type MOD{provider}"
            f"[20:27:55] [main/INFO]: Found mod file DMZCompanions-2.0.5 (3).jar of type MOD{provider}"
            "[20:27:55] [main/INFO]: Found mod file HikariCP-7.1.0.jar of type GAMELIBRARY with provider x\n"
            f"[20:27:56] [main/INFO]: Found mod file appleskin-forge-mc1.20.1-2.5.1.jar of type MOD{provider}"
        )
        self.assertEqual(report.jars, ["appleskin-forge-mc1.20.1-2.5.1.jar", "DMZCompanions-2.0.5 (3).jar"])
        self.assertEqual(report.mods, {})


class MixinCulpritFallbackTests(unittest.TestCase):
    def test_config_prefix_is_the_culprit_without_a_mod_list(self) -> None:
        cause = parse_log(_mixin_log(
            "org.spongepowered.asm.mixin.injection.throwables.InjectionError: Critical injection failure: Redirector "
            "stat(Lcom/dragonminez/common/stats/StatsData;Ljava/lang/String;)D in dmzfoo255.mixins.json:Tooltip"
            "Mixin failed injection check, (0/1) succeeded. Scanned 1 target(s). No refMap loaded.",
            "org.spongepowered.asm.mixin.injection.struct.InjectionInfo.validateTargets(InjectionInfo.java:656) "
            "~[mixin-0.8.5.jar:0.8.5]",
        )).root_cause
        self.assertEqual((cause.culprit_mod, cause.culprit_jar, cause.kind), ("dmzfoo255", None, "mixin"))

    def test_mixin_config_wins_over_the_mixed_into_jar(self) -> None:
        cause = parse_log(_mixin_log(
            "org.spongepowered.asm.mixin.injection.throwables.InvalidInjectionException: Critical injection failure: "
            "@Inject annotation on checkNullPlayer could not find any targets matching 'handleKeyPress' in "
            "com.github.kay9.dragonmounts.client.KeyMappings. Using refmap dragon_mounts_patches.refmap.json "
            "[PREINJECT Applicator Phase -> dragon_mounts_patches.mixins.json:KeyMappingsMixin -> Parse]",
            "com.github.kay9.dragonmounts.client.KeyMappings.<init>(KeyMappings.java:1) "
            "~[dragonmounts-1.20.1-10022.jar%23200!/:?]",
        )).root_cause
        self.assertEqual((cause.culprit_mod, cause.culprit_jar, cause.kind), ("dragon_mounts_patches", None, "mixin"))

    def test_prefix_style_config_and_its_jar(self) -> None:
        cause = parse_log(_mixin_log(
            "org.spongepowered.asm.mixin.injection.throwables.InvalidInjectionException: Critical injection failure: "
            "@Inject annotation on iris$writeIrisConfig could not find any targets matching 'writeToDisk' in "
            "me.jellysquid.mods.sodium.client.gui.SodiumGameOptions. [PREINJECT Applicator Phase -> "
            "mixins.oculus.compat.sodium.json:options.MixinSodiumGameOptions from mod (unknown) -> Parse]",
            "net.irisshaders.iris.compat.Foo.bar(Foo.java:1) ~[oculus-mc1.20.1-1.8.0.jar%23150!/:?]",
        )).root_cause
        self.assertEqual(
            (cause.culprit_mod, cause.culprit_jar, cause.kind), ("oculus", "oculus-mc1.20.1-1.8.0.jar", "mixin")
        )

    def test_from_mod_is_preferred_over_the_config(self) -> None:
        cause = parse_log(_mixin_log(
            "org.spongepowered.asm.mixin.injection.throwables.InjectionError: Critical injection failure: Redirector "
            "x in sharedcfg.mixins.json:FooMixin from mod realmod failed injection check, (0/1) succeeded."
        )).root_cause
        self.assertEqual((cause.culprit_mod, cause.kind), ("realmod", "mixin"))


# Shaped like Forge's "Mod Loading has failed" crash report (synthetic, trimmed).
_FML_CRASH = """\
---- Minecraft Crash Report ----
// Ouch. That hurt :(

Description: Mod loading error has occurred

java.lang.Exception: Mod Loading has failed
\tat net.minecraftforge.logging.CrashReportExtender.dumpModLoadingCrashReport(CrashReportExtender.java:60) ~[forge.jar%23200!/:?] {}
\tat net.minecraft.client.Minecraft.m_91374_(Minecraft.java:718) ~[client-1.20.1-srg.jar%23199!/:?] {}


A detailed walkthrough of the error, its code path and all known details is as follows:
---------------------------------------------------------------------------------------

-- Head --
Thread: Render thread
Suspected Mods: NONE
Stacktrace:
\tat org.spongepowered.asm.mixin.injection.struct.InjectionInfo.validateTargets(InjectionInfo.java:656) ~[mixin-0.8.5.jar%23152!/:0.8.5] {}
-- MOD jei --
Details:
\tMod File: /C:/Users/<user>/mods/jei.jar
\tFailure message: none
Stacktrace:
\tat java.lang.Thread.run(Unknown Source) ~[?:?] {}
-- MOD dragonminez --
Details:
\tCaused by 0: org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: An unexpected critical error was encountered
\t\tat org.spongepowered.asm.mixin.transformer.MixinProcessor.applyMixins(MixinProcessor.java:392) ~[mixin-0.8.5.jar:0.8.5] {}

\tCaused by 1: org.spongepowered.asm.mixin.throwables.MixinApplyError: Mixin [dmzplus.mixins.json:GrantWishZunoGuardMixin] from phase [DEFAULT] in config [dmzplus.mixins.json] FAILED during APPLY
\t\tat com.example.Loader.load(Loader.java:5) ~[example-1.0.jar%2310!/:1.0] {}

\tMod File: /C:/Users/<user>/mods/dragonminez-2.2.jar
\tFailure message: DragonMineZ (dragonminez) has failed to load correctly
\t\torg.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: An unexpected critical error was encountered
\tMod Version: 2.2
\tMod Issue URL: https://discord.dragonminez.com
\tException message: org.spongepowered.asm.mixin.injection.throwables.InvalidInjectionException: Critical injection failure: @Inject annotation on dmzplus$guardZunoRevive could not find any targets matching 'lambda$handle$1' in com.dragonminez.common.network.C2S.GrantWishC2S. No refMap loaded. [PREINJECT Applicator Phase -> dmzplus.mixins.json:GrantWishZunoGuardMixin from mod dmzplus -> Prepare Injections]
Stacktrace:
\tat org.spongepowered.asm.mixin.injection.struct.InjectionInfo.validateTargets(InjectionInfo.java:656) ~[mixin-0.8.5.jar%23152!/:0.8.5] {}

-- System Details --
Details:
\tMinecraft Version: 1.20.1
"""


class ModLoadingCrashTests(unittest.TestCase):
    def test_mod_section_holds_the_root_cause(self) -> None:
        report = parse_log(_FML_CRASH)
        cause = report.root_cause
        self.assertEqual((cause.culprit_mod, cause.kind), ("dmzplus", "mixin"))
        self.assertTrue(cause.exception.startswith(
            "InvalidInjectionException: Critical injection failure: @Inject annotation on dmzplus$guardZunoRevive"
        ))
        self.assertEqual(cause.chain[0], "MixinTransformerError: An unexpected critical error was encountered")
        self.assertTrue(cause.chain[1].startswith("MixinApplyError: Mixin [dmzplus.mixins.json"))
        self.assertEqual(len(cause.chain), 3)
        self.assertEqual(cause.frames, ("com.example.Loader.load",))
        # The card's stacktrace is still the crash block.
        self.assertTrue(report.stacktrace.startswith("Description: Mod loading error has occurred"))

    def test_other_crashes_ignore_mod_sections(self) -> None:
        report = parse_log(_FML_CRASH.replace("java.lang.Exception: Mod Loading has failed", "java.lang.Exception: boom"))
        self.assertEqual(report.root_cause.exception, "Exception: boom")
        self.assertIsNone(report.root_cause.culprit_mod)


class MissingDmzClassTests(unittest.TestCase):
    def test_dmz_frames_are_not_blamed_for_a_missing_dmz_class(self) -> None:
        report = parse_log(
            "---- Minecraft Crash Report ----\n"
            "Description: Rendering entity in world\n"
            "\n"
            "java.lang.NoClassDefFoundError: com/dragonminez/client/render/hair/HairRenderer\n"
            "\tat com.dragonminez.client.render.layer.HairLayer.render(HairLayer.java:10) ~[dragonminez-2.2.jar%23300!/:2.2]\n"
            "Caused by: java.lang.ClassNotFoundException: com.dragonminez.client.render.hair.HairRenderer\n"
            "\tat com.dragonminez.client.render.layer.HairLayer.render(HairLayer.java:10) ~[dragonminez-2.2.jar%23300!/:2.2]\n"
            "Caused by: org.spongepowered.asm.mixin.throwables.ClassMetadataNotFoundException: "
            "com.dragonminez.client.render.hair.HairRenderer\n"
            "\tat org.spongepowered.asm.mixin.transformer.MixinInfo.x(MixinInfo.java:1) ~[mixin-0.8.5.jar:0.8.5]\n"
            "\n"
            "-- System Details --\n"
            "\tMod List: \n"
            "\t\tdragonminez-2.2.jar |DragonMineZ |dragonminez |2.2 |DONE |Manifest: NOSIGNATURE\n"
            "\t\tdmzfoo-1.0.jar |DMZ Foo |dmzfoo |1.0 |DONE |Manifest: NOSIGNATURE\n"
        )
        cause = report.root_cause
        self.assertTrue(cause.exception.startswith("ClassMetadataNotFoundException: com.dragonminez."))
        self.assertEqual((cause.culprit_mod, cause.culprit_jar), (None, None))
        self.assertEqual(len(report.hints), 1)
        self.assertIn("`dmzfoo`", report.hints[0])
