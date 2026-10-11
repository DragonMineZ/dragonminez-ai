import unittest

from bulmaai.cogs.dmzdebug import build_card, summary_for_ai
from bulmaai.utils.dmzdebug_parser import parse_debug

from test_dmzdebug_parser import JSON_DUMP, TEXT_DUMP
from v2_helpers import buttons, section, texts


def _field(view, name_fragment):
    if name_fragment == "Player":  # identity sits under the title
        return texts(view)[0]
    return section(view, name_fragment)


class BuildCardTextTests(unittest.IsolatedAsyncioTestCase):  # py-cord views need a running loop
    async def asyncSetUp(self):
        self.card = build_card(parse_debug(TEXT_DUMP), "Goku.log", file_size=1024)

    def test_title_has_player(self):
        self.assertIn("## 🐉 Goku", texts(self.card)[0])

    def test_core_stats_rendered(self):
        stats = _field(self.card, "Stats")
        self.assertIn("Strength", stats)
        self.assertIn("500", stats)
        self.assertNotIn("JSON", stats)

    def test_identity_shows_race_and_class(self):
        player = _field(self.card, "Player")
        self.assertIn("saiyan", player)
        self.assertIn("warrior", player)

    def test_resources_rendered(self):
        res = _field(self.card, "Resources")
        self.assertIsNotNone(res)
        self.assertIn("Energy", res)
        self.assertIn("Zenkai", res)

    def test_state_shows_dead_and_active_flags(self):
        state = _field(self.card, "State")
        self.assertIn("Dead", state)
        self.assertIn("Fused", state)
        self.assertIn("Aura on", state)

    def test_skills_rendered(self):
        skills = _field(self.card, "Skills")
        self.assertIn("kaioken", skills)
        self.assertIn("3/5", skills)

    def test_techniques_rendered(self):
        tech = _field(self.card, "Techniques")
        self.assertIn("kamehameha", tech)
        self.assertIn("Charging", tech)

    def test_party_desync_surfaced(self):
        party = _field(self.card, "Party")
        self.assertIn("Desync detected", party)

    def test_warnings_panel(self):
        warnings = _field(self.card, "Warnings")
        self.assertIsNotNone(warnings)
        self.assertIn("dead", warnings)

    def test_quest_objectives_shown(self):
        quests = _field(self.card, "Quests")
        self.assertIn("saiyan_saga:3", quests)
        self.assertIn("4/10", quests)

    def test_card_fits_the_v2_text_limit(self):
        self.assertLessEqual(sum(map(len, texts(self.card))), 4000)
        self.assertTrue(texts(self.card)[-1].startswith("-# 📄 Goku.log"))


# The v2.2 dump prints SNBT values: 1b booleans, 0.0f/5.0d numbers.
NBT_DUMP = """##############
## DMZ DEBUG ##
## Vegeta ##
##############

# Resources:
CurrentEnergy: 44020.0f
TrainingPointsD: 5291005.0d

# Status:
IsAlive: 1b
TailVisible: 1b
IsStunned: 0b

# Character:
CurrentForm: supersaiyan4
CurrentFormGroup: supersaiyan
SelectedStackForm: x2
SelectedStackFormGroup: kaioken
FormMasteries:
  supersaiyan:supersaiyan2: 100.0d
  godritual:supersaiyangodritual: 0.0d
"""


class BuildCardNbtTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.report = parse_debug(NBT_DUMP)
        self.card = build_card(self.report, "Vegeta.log", page_url="https://logs.example/l/" + "a" * 32)

    def test_snbt_values_read_as_numbers_and_flags(self):
        self.assertIn("**Energy** 44,020", _field(self.card, "Resources"))
        self.assertIn("**TP** 5,291,005", _field(self.card, "Resources"))
        state = _field(self.card, "State")
        self.assertIn("Alive", state)
        self.assertIn("Tail visible", state)
        self.assertNotIn("Stunned", state)

    def test_forms_show_stack_and_masteries_below_100(self):
        forms = _field(self.card, "Forms")
        self.assertIn("`kaioken` / `x2` (selected)", forms)
        self.assertIn("1/2 at 100", forms)
        self.assertIn("godritual:supersaiyangodritual", forms)

    def test_full_dump_button_links_the_page(self):
        self.assertEqual([b.url for b in buttons(self.card)], ["https://logs.example/l/" + "a" * 32])

    def test_ai_summary_is_plain_text_of_the_sections(self):
        summary = summary_for_ai(self.report)
        self.assertIn("Vegeta", summary)
        self.assertIn("supersaiyan4", summary)
        self.assertLessEqual(len(summary), 2000)


class BuildCardJsonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.card = build_card(parse_debug(JSON_DUMP), "Steve.json", file_size=2048)

    def test_title_has_player(self):
        self.assertIn("## 🐉 Steve", texts(self.card)[0])

    def test_identity_has_versions(self):
        player = _field(self.card, "Player")
        self.assertIn("2.1.3", player)
        self.assertIn("dragonminez:kaio", player)

    def test_derived_battlepower(self):
        stats = _field(self.card, "Stats")
        self.assertIn("BP", stats)
        self.assertIn("1,234,567", stats)

    def test_quest_objectives_from_json(self):
        quests = _field(self.card, "Quests")
        self.assertIn("4/10", quests)

    def test_no_crash_without_party(self):
        # JSON dump has no hand-written party section; card should still build.
        self.assertIsNone(_field(self.card, "Party"))


if __name__ == "__main__":
    unittest.main()
