import unittest
from unittest import mock

from bulmaai.utils import crash_hints
from bulmaai.utils.crash_hints import MAX_HINT_LEN, Rule, hints_for
from bulmaai.utils.log_parser import LogReport, RootCause


def _report(
    *chain: str,
    culprit_mod: str | None = None,
    culprit_jar: str | None = None,
    frames: tuple[str, ...] = (),
    **fields,
) -> LogReport:
    cause = RootCause(chain[-1], chain, culprit_mod, culprit_jar, "exception", frames) if chain else None
    return LogReport(root_cause=cause, **fields)


_DMZ_NO_METHOD = "NoSuchMethodError: 'double com.dragonminez.common.config.CombatConfig.getDefenseReductionScale()'"


class AddonOutdatedTests(unittest.TestCase):
    def test_names_the_culprit(self) -> None:
        hints = hints_for(_report(_DMZ_NO_METHOD, culprit_mod="dmzcompanions", dragonminez_version="2.2.1"))
        self.assertEqual(len(hints), 1)
        self.assertTrue(hints[0].startswith("`dmzcompanions` uses DragonMineZ code that isn't in DMZ 2.2.1"))

    def test_mixin_miss_on_dmz_code(self) -> None:
        hints = hints_for(_report(
            "InvalidInjectionException: Critical injection failure: @Inject annotation on onTickGame could not find "
            "any targets matching 'tickGame' in com.dragonminez.client.gui.ControlGameScreen.",
            culprit_mod="dragonminez_autotraining",
        ))
        self.assertEqual(len(hints), 1)
        self.assertIn("`dragonminez_autotraining`", hints[0])
        self.assertIn("this DMZ version", hints[0])

    def test_without_culprit_lists_the_installed_addons(self) -> None:
        mods = {"dragonminez": "2.2", "dmzsparking": "1.5", "jei": "15", "dmzsuper": "1.9"}
        hint = hints_for(_report(
            "ClassMetadataNotFoundException: com.dragonminez.client.render.hair.HairRenderer", mods=mods
        ))[0]
        self.assertIn("Likely one of your DMZ addons: `dmzsparking`, `dmzsuper`.", hint)
        self.assertNotIn("`dragonminez`", hint)

    def test_without_mod_list_uses_jar_names(self) -> None:
        hint = hints_for(_report(
            "InjectionError: Redirector stat(Lcom/dragonminez/common/stats/StatsData;)D in x failed injection check",
            jars=["dragonminez-2.2.jar", "dmzhollow255-1.0.jar", "jei-15.jar"],
        ))[0]
        self.assertIn("`dmzhollow255`", hint)
        self.assertNotIn("`dragonminez`", hint)

    def test_without_culprit_or_addons(self) -> None:
        hint = hints_for(_report(_DMZ_NO_METHOD))[0]
        self.assertTrue(hint.startswith("One of your DMZ addons uses DragonMineZ code"))

    def test_long_addon_lists_are_cut_to_fit(self) -> None:
        mods = {f"dmz_{'x' * 50}_{i}": "1" for i in range(5)}
        hint = hints_for(_report(_DMZ_NO_METHOD, mods=mods))[0]
        self.assertLessEqual(len(hint), MAX_HINT_LEN)
        self.assertIn("Likely one of your DMZ addons", hint)

    def test_plain_dmz_crash_gets_no_hint(self) -> None:
        npe = 'NullPointerException: Cannot invoke "com.dragonminez.server.HeldVictim.release()" because "x" is null'
        self.assertEqual(hints_for(_report(npe, culprit_mod="dragonminez")), [])
        self.assertEqual(hints_for(_report(npe)), [])

    def test_dragonminez_itself_gets_no_hint(self) -> None:
        self.assertEqual(hints_for(_report(_DMZ_NO_METHOD, culprit_mod="dragonminez")), [])
        self.assertEqual(hints_for(_report(_DMZ_NO_METHOD, culprit_jar="dragonminez-2.2.1.jar")), [])

    def test_missing_class_outside_dmz_gets_no_hint(self) -> None:
        cnf = "ClassNotFoundException: traben.entity_model_features.models.Foo"
        self.assertEqual(hints_for(_report(cnf, culprit_mod="alltheleaks")), [])


class McVersionMismatchTests(unittest.TestCase):
    _NO_METHOD = "NoSuchMethodError: 'net.minecraft.resources.ResourceLocation net.minecraft.resources.Foo.bar()'"

    def test_names_the_culprit_and_versions(self) -> None:
        hints = hints_for(_report(
            self._NO_METHOD, culprit_mod="somemod", mc_version="1.20.1", forge_version="47.4.10"
        ))
        self.assertEqual(len(hints), 1)
        self.assertIn("`somemod` was built for a different Minecraft/Forge version", hints[0])
        self.assertIn("(Minecraft 1.20.1 / Forge 47.4.10)", hints[0])

    def test_needs_a_non_dmz_culprit(self) -> None:
        self.assertEqual(hints_for(_report(self._NO_METHOD)), [])
        self.assertEqual(hints_for(_report(self._NO_METHOD, culprit_mod="dragonminez")), [])


class OtherRuleTests(unittest.TestCase):
    def test_rubidium(self) -> None:
        self.assertIn("Rubidium", hints_for(_report(mods={"rubidium": "0.6.5"}))[0])
        self.assertIn("Rubidium", hints_for(_report(jars=["rubidium-mc1.20.1-0.7.1a.jar"]))[0])
        # Embeddium bundles a "rubidium" stub; that's not Rubidium.
        self.assertEqual(hints_for(_report(mods={"embeddium": "0.3.31", "rubidium": "0.7.1"})), [])

    def test_locale_digits(self) -> None:
        bad = "ResourceLocationException: Non [a-z0-9/._-] character in path of location: minecraft:textures/x_{}.png"
        self.assertIn("-Duser.language=en", hints_for(_report(bad.format("\\u0661")))[0])
        self.assertIn("-Duser.language=en", hints_for(_report(bad.format("১")))[0])
        self.assertEqual(hints_for(_report(bad.format("X"))), [])

    def test_jer_capsule_trade(self) -> None:
        npe = 'NullPointerException: Cannot invoke "net.minecraft.world.entity.Entity.m_9236_()" because "trader" is null'
        frames = ("com.dragonminez.common.init.CapsuleCorpMapTrade.m_213663_", "jeresources.collection.TradeList.add")
        self.assertIn("Just Enough Resources", hints_for(_report(npe, frames=frames))[0])
        self.assertEqual(hints_for(_report(npe, frames=("com.other.Trade.get",))), [])

    def test_sparking_super(self) -> None:
        text = "IllegalStateException: DMZ Sparking cannot run alongside DragonMineZ Super (dmzsuper-1.9.0.jar)"
        self.assertEqual(
            hints_for(_report(text)), ["DMZ Sparking and DragonMineZ Super can't be installed together. Remove one of them."]
        )
        self.assertEqual(hints_for(_report("IllegalStateException: Already building!")), [])

    def test_missing_dmz_asset(self) -> None:
        missing = "FileNotFoundException: dragonminez:textures/entity/races/x/x_layer1.png"
        self.assertIn("DMZ 2.2 doesn't have", hints_for(_report(missing, dragonminez_version="2.2"))[0])
        gecko = "GeckoLibException: dragonminez:animations/entity/master.animation.json: Unable to find animation file."
        self.assertIn("your DMZ version doesn't have", hints_for(_report(gecko))[0])
        self.assertEqual(hints_for(_report("FileNotFoundException: minecraft:textures/x.png")), [])


class HintListTests(unittest.TestCase):
    def test_hints_are_short_and_capped(self) -> None:
        report = _report(
            "IllegalStateException: DMZ Sparking cannot run alongside DragonMineZ Super",
            "FileNotFoundException: dragonminez:textures/x.png",
            _DMZ_NO_METHOD,
            culprit_mod="dmzfoo",
            mods={"rubidium": "0.6.5"},
        )
        hints = hints_for(report)
        self.assertEqual(len(hints), 3)
        self.assertTrue(all(len(h) <= MAX_HINT_LEN for h in hints))
        self.assertFalse(any("Rubidium" in h for h in hints))

    def test_duplicates_are_dropped(self) -> None:
        same = Rule("a", lambda r: True, lambda r: "Same text.")
        with mock.patch.object(crash_hints, "RULES", (same, same._replace(name="b"))):
            self.assertEqual(hints_for(LogReport()), ["Same text."])

    def test_clean_report_has_no_hints(self) -> None:
        self.assertEqual(hints_for(LogReport()), [])
