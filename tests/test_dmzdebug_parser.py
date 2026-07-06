import json
import unittest

from bulmaai.utils.dmzdebug_parser import (
    as_bool,
    as_number,
    detect_format,
    looks_like_dmzdebug,
    parse_debug,
    ticks_to_seconds,
)
from bulmaai.utils import dmzdebug_diagnostics as diag


# A representative ALL-scope text dump exercising every block type: NBT leaves,
# nested compounds, lists of compounds, and the hand-written Party/Quests
# sections — with a deliberate client-vs-server party desync.
TEXT_DUMP = """\
##############
## DMZ DEBUG ##
## Goku ##
##############

# Stats:
ENE: 400
PWR: 600
RES: 300
SKP: 200
STR: 500
VIT: 100

# BonusStats:
STR:
  - 0:
    ApplyMultipliers: 1
    Name: zenkai
    Operation: ADDITION
    Value: 50.0

# Resources:
Alignment: -50
CurrentEnergy: 250.0
CurrentPoise: 80.0
CurrentStamina: 120.0
PendingAttributePoints: 3
Release: 100
ReleaseLimit: 100
ZenkaiCount: 2

# Status:
AndroidUpgraded: 1
AuraActive: 1
IsAlive: 0
IsBlocking: 0
IsFused: 1

# Cooldowns:
Drain: 0
SenzuKarin: 200
Zenkai: 40

# Effects:
EffectsList:
  - 0:
    Duration: 200
    Name: poison
    Power: 2.0
  - 1:
    Duration: 100
    Name: regen
    Power: 1.0

# Skills:
SkillsList:
  - 0:
    IsActive: 1
    Level: 3
    MaxLevel: 5
    Name: kaioken
  - 1:
    IsActive: 0
    Level: 1
    MaxLevel: 10
    Name: fly

# Character:
Class: warrior
CurrentForm: ssj2
CurrentFormGroup: ssj
Gender: male
PreviousForm: base
Race: saiyan

# Techniques:
ChargeHolding: 0
ChargingTechniqueId: kamehameha
EquippedSlots:
  Slot0: kamehameha
  Slot1:
  Slot2: galick_gun
SelectedSlot: 0
TechniqueChargePercent: 75.0
TechniqueCharging: 1

# Party:
In party (client view): true
Active party id (client): 11111111-1111-1111-1111-111111111111
Party leader (client): 22222222-2222-2222-2222-222222222222 (Goku)
Party members (client) [2]:
  - 22222222-2222-2222-2222-222222222222 (Goku)
  - 33333333-3333-3333-3333-333333333333 (Vegeta)
Pending invite: none
Server party (PartySavedData): not in a server party
Server PvP enabled: false
Server members [0]:
  (none)

# Quests:
Tracked quest: saiyan_saga:3
Last completed quest: saiyan_saga:2 (status SUCCESS)
Completed [1]:
  - saiyan_saga:2
In progress (accepted) [1]:
  - saiyan_saga:3
Failed [0]:
  (none)

# PlayerQuestData (raw):
difficulty: NORMAL
difficultyChosen: 1
questState:
  quests:
    - 0:
      failureCount: 0
      objectiveRequirements:
        0: 10
        1: 1
      objectives:
        0: 4
        1: 0
      questId: saiyan_saga:3
      rewards:
        0: 0
      status: ACCEPTED
  trackedQuestId: saiyan_saga:3
"""


JSON_DUMP = json.dumps(
    {
        "schemaVersion": 1,
        "generatedAt": "2026-07-06T12:34:56Z",
        "mod": {"version": "2.1.3", "mc": "1.20.1", "forge": "47.x"},
        "server": {"dedicated": True},
        "player": {
            "name": "Steve",
            "uuid": "1e5f-uuid",
            "online": True,
            "dimension": "dragonminez:kaio",
            "pos": [12.0, 64.0, -30.0],
        },
        "derived": {
            "battlePower": 1234567.0,
            "level": 42,
            "maxHealth": 800.0,
            "currentForm": "ssj2",
            "currentFormGroup": "ssj",
        },
        "scope": "ALL",
        "sections": {
            "Stats": {"STR": 500, "SKP": 200},
            "Character": {"Race": "saiyan"},
            "PlayerQuestData": {
                "questState": {
                    "trackedQuestId": "saiyan_saga:3",
                    "quests": [
                        {
                            "questId": "saiyan_saga:3",
                            "status": "ACCEPTED",
                            "objectives": {"0": 4, "1": 0},
                            "objectiveRequirements": {"0": 10, "1": 1},
                            "rewards": {"0": False},
                        }
                    ],
                }
            },
        },
    }
)


class DetectionTests(unittest.TestCase):
    def test_detects_text_format(self):
        self.assertEqual(detect_format(TEXT_DUMP), "text")
        self.assertTrue(looks_like_dmzdebug(TEXT_DUMP))

    def test_detects_json_format(self):
        self.assertEqual(detect_format(JSON_DUMP), "json")
        self.assertTrue(looks_like_dmzdebug(JSON_DUMP))

    def test_rejects_unrelated_content(self):
        self.assertIsNone(detect_format("just some random log line\nanother"))
        self.assertFalse(looks_like_dmzdebug("just some random log line"))
        self.assertFalse(looks_like_dmzdebug('{"foo": 1}'))


class TextParserTests(unittest.TestCase):
    def setUp(self):
        self.report = parse_debug(TEXT_DUMP)

    def test_header_player_name(self):
        self.assertEqual(self.report.player_name, "Goku")
        self.assertEqual(self.report.fmt, "text")

    def test_flat_stats_leaves(self):
        stats = self.report.sections["Stats"]
        self.assertEqual(stats["STR"], "500")
        self.assertEqual(stats["ENE"], "400")

    def test_status_booleans_are_stringified(self):
        status = self.report.sections["Status"]
        self.assertEqual(status["IsAlive"], "0")
        self.assertIs(as_bool(status["IsAlive"]), False)
        self.assertIs(as_bool(status["AndroidUpgraded"]), True)

    def test_bonusstats_list_of_compounds(self):
        bonus = self.report.sections["BonusStats"]
        self.assertIn("STR", bonus)
        self.assertIsInstance(bonus["STR"], list)
        self.assertEqual(bonus["STR"][0]["Name"], "zenkai")
        self.assertEqual(bonus["STR"][0]["Value"], "50.0")

    def test_effects_nested_list(self):
        effects = self.report.sections["Effects"]["EffectsList"]
        self.assertEqual(len(effects), 2)
        self.assertEqual(effects[0]["Name"], "poison")
        self.assertEqual(effects[1]["Duration"], "100")

    def test_resources_flat(self):
        res = self.report.sections["Resources"]
        self.assertEqual(res["CurrentEnergy"], "250.0")
        self.assertEqual(res["Alignment"], "-50")
        self.assertEqual(res["ZenkaiCount"], "2")

    def test_skills_list(self):
        skills = self.report.sections["Skills"]["SkillsList"]
        self.assertEqual(len(skills), 2)
        self.assertEqual(skills[0]["Name"], "kaioken")
        self.assertEqual(skills[0]["Level"], "3")
        self.assertIs(as_bool(skills[0]["IsActive"]), True)

    def test_techniques_equipped_slots(self):
        tech = self.report.sections["Techniques"]
        self.assertEqual(tech["EquippedSlots"]["Slot0"], "kamehameha")
        self.assertEqual(tech["EquippedSlots"]["Slot2"], "galick_gun")
        self.assertIs(as_bool(tech["TechniqueCharging"]), True)
        self.assertEqual(tech["TechniqueChargePercent"], "75.0")

    def test_character_forms(self):
        char = self.report.sections["Character"]
        self.assertEqual(char["CurrentFormGroup"], "ssj")
        self.assertEqual(char["CurrentForm"], "ssj2")
        self.assertEqual(char["PreviousForm"], "base")
        self.assertEqual(char["Race"], "saiyan")
        self.assertEqual(char["Class"], "warrior")

    def test_party_client_and_server(self):
        party = self.report.party
        self.assertTrue(party["client"]["in_party"])
        self.assertEqual(len(party["client"]["members"]), 2)
        self.assertEqual(party["client"]["members"][1]["name"], "Vegeta")
        self.assertFalse(party["server"]["in_party"])
        self.assertEqual(party["server"]["members"], [])

    def test_quests_handwritten(self):
        q = self.report.quests
        self.assertEqual(q["tracked"], "saiyan_saga:3")
        self.assertEqual(q["last_completed"], {"quest_id": "saiyan_saga:2", "status": "SUCCESS"})
        self.assertEqual(q["completed"], ["saiyan_saga:2"])
        self.assertEqual(q["in_progress"], ["saiyan_saga:3"])
        self.assertEqual(q["failed"], [])

    def test_playerquestdata_raw_nested(self):
        raw = self.report.sections["PlayerQuestData (raw)"]
        self.assertEqual(raw["difficulty"], "NORMAL")
        quest = raw["questState"]["quests"][0]
        self.assertEqual(quest["questId"], "saiyan_saga:3")
        self.assertEqual(quest["objectives"], {"0": "4", "1": "0"})
        self.assertEqual(quest["objectiveRequirements"], {"0": "10", "1": "1"})
        self.assertEqual(quest["rewards"], {"0": "0"})


class JsonParserTests(unittest.TestCase):
    def setUp(self):
        self.report = parse_debug(JSON_DUMP)

    def test_metadata(self):
        self.assertEqual(self.report.fmt, "json")
        self.assertEqual(self.report.schema_version, 1)
        self.assertEqual(self.report.player_name, "Steve")
        self.assertEqual(self.report.player_uuid, "1e5f-uuid")
        self.assertTrue(self.report.online)
        self.assertEqual(self.report.mod_version, "2.1.3")
        self.assertTrue(self.report.dedicated)

    def test_derived_and_sections(self):
        self.assertEqual(self.report.derived["battlePower"], 1234567.0)
        self.assertEqual(self.report.sections["Stats"]["STR"], 500)

    def test_newer_schema_warns(self):
        newer = JSON_DUMP.replace('"schemaVersion": 1', '"schemaVersion": 2')
        report = parse_debug(newer)
        self.assertTrue(any("newer than supported" in w for w in report.warnings))


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.text = parse_debug(TEXT_DUMP)
        self.json = parse_debug(JSON_DUMP)

    def test_party_desync_flagged(self):
        flags = diag.detect_party_desync(self.text)
        self.assertTrue(any("no server party" in f for f in flags))

    def test_warnings_dead_and_android(self):
        warnings = diag.collect_warnings(self.text)
        self.assertTrue(any("dead" in w for w in warnings))
        self.assertTrue(any("Battle Power to max" in w for w in warnings))

    def test_active_cooldowns_converted_to_seconds(self):
        cds = dict(diag.summarize_cooldowns(self.text))
        self.assertNotIn("Drain", cds)  # value 0 dropped
        self.assertEqual(cds["SenzuKarin"], 10.0)  # 200 ticks / 20
        self.assertEqual(cds["Zenkai"], 2.0)

    def test_tracked_quest_objectives_text(self):
        obj = diag.tracked_quest_objectives(self.text)
        self.assertEqual(obj["quest_id"], "saiyan_saga:3")
        self.assertEqual(obj["status"], "ACCEPTED")
        self.assertEqual(obj["objectives"], [("0", "4", "10"), ("1", "0", "1")])
        self.assertEqual(obj["unclaimed"], ["0"])

    def test_tracked_quest_objectives_json(self):
        obj = diag.tracked_quest_objectives(self.json)
        self.assertEqual(obj["quest_id"], "saiyan_saga:3")
        self.assertEqual(obj["objectives"], [("0", 4, 10), ("1", 0, 1)])
        self.assertEqual(obj["unclaimed"], ["0"])


class ValueHelperTests(unittest.TestCase):
    def test_as_bool(self):
        self.assertIs(as_bool("1"), True)
        self.assertIs(as_bool("0"), False)
        self.assertIs(as_bool(True), True)
        self.assertIsNone(as_bool("saiyan"))

    def test_as_number(self):
        self.assertEqual(as_number("250.0"), 250.0)
        self.assertEqual(as_number(42), 42.0)
        self.assertIsNone(as_number("abc"))
        self.assertIsNone(as_number(True))

    def test_ticks_to_seconds(self):
        self.assertEqual(ticks_to_seconds("40"), 2.0)
        self.assertEqual(ticks_to_seconds(10), 0.5)
        self.assertIsNone(ticks_to_seconds("x"))


class RobustnessTests(unittest.TestCase):
    def test_empty_section_is_harmless(self):
        dump = (
            "##############\n## DMZ DEBUG ##\n## Bob ##\n##############\n\n"
            "# BonusStats:\n(empty)\n\n"
            "# Stats:\nSTR: 1\n"
        )
        report = parse_debug(dump)
        self.assertEqual(report.sections["BonusStats"], {})
        self.assertEqual(report.sections["Stats"]["STR"], "1")

    def test_missing_sections_do_not_crash(self):
        dump = "##############\n## DMZ DEBUG ##\n## Bob ##\n##############\n\n# Character:\nRace: namekian\n"
        report = parse_debug(dump)
        self.assertEqual(report.sections["Character"]["Race"], "namekian")
        self.assertIsNone(report.party)
        self.assertEqual(diag.detect_party_desync(report), [])

    def test_empty_string_leaf_is_not_a_container(self):
        dump = (
            "##############\n## DMZ DEBUG ##\n## Bob ##\n##############\n\n"
            "# Techniques:\nChargingTechniqueId: \nSelectedSlot: 3\n"
        )
        report = parse_debug(dump)
        tech = report.sections["Techniques"]
        self.assertEqual(tech["ChargingTechniqueId"], "")
        self.assertEqual(tech["SelectedSlot"], "3")


if __name__ == "__main__":
    unittest.main()
