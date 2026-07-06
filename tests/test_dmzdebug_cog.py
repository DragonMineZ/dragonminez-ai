import unittest

from bulmaai.cogs.dmzdebug import build_embed
from bulmaai.utils.dmzdebug_parser import parse_debug

from test_dmzdebug_parser import JSON_DUMP, TEXT_DUMP


def _field(embed, name_fragment):
    for f in embed.fields:
        if name_fragment in f.name:
            return f.value
    return None


class BuildEmbedTextTests(unittest.TestCase):
    def setUp(self):
        self.embed = build_embed(parse_debug(TEXT_DUMP), "Goku.log", file_size=1024)

    def test_title_has_player(self):
        self.assertIn("Goku", self.embed.title)

    def test_legacy_note_for_text(self):
        self.assertIn("Legacy text dump", _field(self.embed, "Player"))

    def test_core_stats_rendered(self):
        stats = _field(self.embed, "Stats")
        self.assertIn("Strength", stats)
        self.assertIn("500", stats)
        self.assertIn("need the JSON dump", stats)

    def test_identity_shows_race_and_class(self):
        player = _field(self.embed, "Player")
        self.assertIn("saiyan", player)
        self.assertIn("warrior", player)

    def test_resources_rendered(self):
        res = _field(self.embed, "Resources")
        self.assertIsNotNone(res)
        self.assertIn("Energy", res)
        self.assertIn("Zenkai", res)

    def test_state_shows_dead_and_active_flags(self):
        state = _field(self.embed, "State")
        self.assertIn("Dead", state)
        self.assertIn("Fused", state)
        self.assertIn("Aura on", state)

    def test_skills_rendered(self):
        skills = _field(self.embed, "Skills")
        self.assertIn("kaioken", skills)
        self.assertIn("3/5", skills)

    def test_techniques_rendered(self):
        tech = _field(self.embed, "Techniques")
        self.assertIn("kamehameha", tech)
        self.assertIn("Charging", tech)

    def test_party_desync_surfaced(self):
        party = _field(self.embed, "Party")
        self.assertIn("Desync detected", party)

    def test_warnings_panel(self):
        warnings = _field(self.embed, "Warnings")
        self.assertIsNotNone(warnings)
        self.assertIn("dead", warnings)

    def test_quest_objectives_shown(self):
        quests = _field(self.embed, "Quests")
        self.assertIn("saiyan_saga:3", quests)
        self.assertIn("4/10", quests)

    def test_all_field_values_within_discord_limits(self):
        for f in self.embed.fields:
            self.assertTrue(f.value)
            self.assertLessEqual(len(f.value), 1024)
        self.assertLessEqual(len(self.embed), 6000)


class BuildEmbedJsonTests(unittest.TestCase):
    def setUp(self):
        self.embed = build_embed(parse_debug(JSON_DUMP), "Steve.json", file_size=2048)

    def test_title_has_player(self):
        self.assertIn("Steve", self.embed.title)

    def test_identity_has_versions(self):
        player = _field(self.embed, "Player")
        self.assertIn("2.1.3", player)
        self.assertIn("dragonminez:kaio", player)

    def test_derived_battlepower(self):
        stats = _field(self.embed, "Stats")
        self.assertIn("BP", stats)
        self.assertIn("1,234,567", stats)

    def test_quest_objectives_from_json(self):
        quests = _field(self.embed, "Quests")
        self.assertIn("4/10", quests)

    def test_no_crash_without_party(self):
        # JSON dump has no hand-written party section; embed should still build.
        self.assertIsNone(_field(self.embed, "Party"))


if __name__ == "__main__":
    unittest.main()
