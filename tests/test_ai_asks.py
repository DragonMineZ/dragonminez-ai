import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.ui import ai_asks
from bulmaai.ui.v2 import TEXT_LIMIT

SETTINGS = SimpleNamespace(
    moderation_banned_words=("slur*",),
    moderation_allowed_domains=(),
    moderation_blocked_domains=("evil.example",),
    wiki_base_url="https://wiki.dragonminez.com",
)


def validate(**args):
    return ai_asks.validate_ask(args, SETTINGS)


class ValidateAskTests(unittest.TestCase):
    def test_choices_keep_distinct_screened_labels_up_to_five(self) -> None:
        out = validate(
            kind="choices",
            choices=["Stats", " stats ", "latest.log", "<@1>", "@everyone", "slurx", "x" * 81, "Raza", "A", "B", "C"],
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["shown"], {"kind": "choices", "choices": ["Stats", "latest.log", "Raza", "A", "B"]})
        self.assertEqual(len(out["dropped"]), 6)  # duplicate, 2 pings, banned word, too long, the sixth choice
        self.assertIn("write the reply", out["note"])

    def test_nothing_usable_is_an_error_the_model_can_fix(self) -> None:
        self.assertEqual(validate(kind="choices", choices=["Only one"])["ok"], False)
        self.assertIn("2-5", validate(kind="choices", choices=["Only one"])["error"])
        self.assertIn("kind must be", validate(kind="steps")["error"])
        self.assertIn("kind must be", validate()["error"])
        self.assertFalse(validate(kind="select", options="1.20.1")["ok"])

    def test_select_options_descriptions_and_placeholder(self) -> None:
        out = validate(
            kind="select",
            placeholder="Your Minecraft version",
            options=[
                {"label": "1.20.1", "description": "Most players"},
                "1.21",
                {"label": "1.19", "description": "see https://evil.example/x"},
                *({"label": f"v{n}"} for n in range(30)),
            ],
        )
        shown = out["shown"]
        self.assertEqual(len(shown["options"]), 25)
        self.assertEqual(shown["options"][:3], [{"label": "1.20.1", "description": "Most players"}, {"label": "1.21"}, {"label": "1.19"}])
        self.assertEqual(shown["placeholder"], "Your Minecraft version")
        self.assertIn("description of option '1.19'", out["dropped"])

    def test_form_fields_per_type(self) -> None:
        out = validate(
            kind="form",
            title="Your setup",
            fields=[
                {"label": "Minecraft version", "type": "select", "options": ["1.20.1", "1.21"], "required": False},
                {"label": "What happens", "type": "long", "placeholder": "Steps to reproduce", "required": False},
                {"label": "Launcher", "type": "radio", "options": [f"L{n}" for n in range(12)], "placeholder": "x"},
                {"label": "minecraft version", "type": "short"},  # duplicate label
                {"label": "Only one", "type": "select", "options": ["a"]},
                {"label": "Bad type", "type": "file"},
                {"label": "x" * 46, "type": "short"},
                {"label": "Mods", "required": False},
                {"label": "Java", "type": "short", "required": False},
                {"label": "Sixth", "type": "short"},
            ],
        )
        fields = out["shown"]["fields"]
        self.assertEqual([field["label"] for field in fields], ["Minecraft version", "What happens", "Launcher", "Mods", "Java"])
        self.assertEqual(fields[1], {"label": "What happens", "type": "long", "required": False, "placeholder": "Steps to reproduce"})
        self.assertEqual((len(fields[2]["options"]), "placeholder" in fields[2], fields[2]["required"]), (10, False, True))
        self.assertEqual(fields[3]["type"], "short")  # no type: short text
        self.assertEqual(out["shown"]["title"], "Your setup")

    def test_a_form_always_asks_for_something_and_needs_a_title(self) -> None:
        out = validate(kind="form", title="Hi", fields=[{"label": "A", "type": "short", "required": False}])
        self.assertTrue(out["shown"]["fields"][0]["required"])
        self.assertIn("title", validate(kind="form", fields=[{"label": "A", "type": "short"}])["error"])
        self.assertIn("title", validate(kind="form", title="x" * 46, fields=[{"label": "A"}])["error"])
        self.assertIn("title", validate(kind="form", title="slurring", fields=[{"label": "A"}])["error"])
        self.assertIn("fields", validate(kind="form", title="Hi", fields=[])["error"])


class ControlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_per_kind(self) -> None:
        (row,) = ai_asks.ask_rows({"kind": "choices", "choices": ["Stats", "Raza"]}, 42, "en")
        self.assertEqual(
            [(item.label, item.custom_id, item.style) for item in row.children],
            [("Stats", "ai_ask:choices:42:0", discord.ButtonStyle.secondary), ("Raza", "ai_ask:choices:42:1", discord.ButtonStyle.secondary)],
        )

        spec = {"kind": "select", "placeholder": "Version", "options": [{"label": "1.20.1", "description": "Most"}, {"label": "1.21"}]}
        (row,) = ai_asks.ask_rows(spec, 42, "en")
        (select,) = row.children
        self.assertEqual((select.custom_id, select.placeholder, select.min_values, select.max_values), ("ai_ask:select:42:0", "Version", 1, 1))
        self.assertEqual([(o.label, o.value, o.description) for o in select.options], [("1.20.1", "0", "Most"), ("1.21", "1", None)])

        (row,) = ai_asks.ask_rows({"kind": "form", "title": "T", "fields": []}, 42, "es")
        (button,) = row.children
        self.assertEqual((button.label, str(button.emoji), button.custom_id), ("Rellenar", "📝", "ai_ask:form:42:0"))
        self.assertEqual(ai_asks.ask_rows({"kind": "steps"}, 42, "en"), [])

    def test_custom_ids_round_trip(self) -> None:
        for kind in ai_asks.KINDS:
            value = ai_asks.custom_id(kind, 123456789012345678, 4)
            self.assertLessEqual(len(value), 100)
            self.assertEqual(ai_asks.parse_custom_id(value), (kind, 123456789012345678, 4))
        for bad in ("ai_ask:steps:42:0", "ai_ask:choices:x:0", "ai_ask:choices:42", "ticket_resolved:yes:42:en"):
            self.assertIsNone(ai_asks.parse_custom_id(bad))
        self.assertEqual(ai_asks.parse_form_modal_id(ai_asks.form_modal_id(55)), 55)
        self.assertIsNone(ai_asks.parse_form_modal_id("ai_ask_form:x"))
        self.assertIsNone(ai_asks.parse_form_modal_id("ai_ask:form:42:0"))

    def test_picked_label(self) -> None:
        choices = {"kind": "choices", "choices": ["a", "b"]}
        select = {"kind": "select", "options": [{"label": "x"}, {"label": "y"}]}
        self.assertEqual(ai_asks.picked_label(choices, "choices", 1), "b")
        self.assertEqual(ai_asks.picked_label(select, "select", 0, ["1"]), "y")
        for spec, kind, index, values in ((choices, "choices", 2, None), (choices, "choices", -1, None), (choices, "select", 0, ["0"]),
                                          (select, "select", 0, ["9"]), (select, "select", 0, []), (select, "select", 0, ["x"])):
            self.assertIsNone(ai_asks.picked_label(spec, kind, index, values))


FORM = {
    "kind": "form",
    "title": "Your setup",
    "fields": [
        {"label": "Version", "type": "select", "options": ["1.20.1", "1.21"], "required": True, "placeholder": "Pick"},
        {"label": "What happens", "type": "long", "required": False, "placeholder": "Steps"},
        {"label": "Launcher", "type": "radio", "options": ["CurseForge", "Prism"], "required": True},
        {"label": "Name", "type": "short", "required": False},
    ],
}


class FormTests(unittest.IsolatedAsyncioTestCase):
    async def test_modal_fields_map_to_label_children(self) -> None:
        modal = ai_asks.form_modal(FORM, 55)
        self.assertEqual((modal.custom_id, modal.title, modal._store), ("ai_ask_form:55", "Your setup", False))
        self.assertTrue(all(isinstance(child, discord.ui.Label) for child in modal.children))
        self.assertEqual([child.label for child in modal.children], ["Version", "What happens", "Launcher", "Name"])
        version, long_text, launcher, name = (child.item for child in modal.children)
        self.assertIsInstance(version, discord.ui.Select)
        self.assertEqual(([o.value for o in version.options], version.placeholder, version.min_values), (["0", "1"], "Pick", 1))
        self.assertIsInstance(long_text, discord.ui.InputText)
        self.assertEqual(
            (long_text.style, long_text.required, long_text.max_length, long_text.placeholder),
            (discord.InputTextStyle.long, False, ai_asks.LONG_VALUE_LIMIT, "Steps"),
        )
        self.assertIsInstance(launcher, discord.ui.RadioGroup)
        self.assertEqual([o.label for o in launcher.options], ["CurseForge", "Prism"])
        self.assertEqual((name.style, name.required, name.max_length), (discord.InputTextStyle.short, False, ai_asks.SHORT_VALUE_LIMIT))
        self.assertEqual([child.item.custom_id for child in modal.children], ["f0", "f1", "f2", "f3"])
        self.assertEqual([part["type"] for part in modal.to_dict()["components"]], [18, 18, 18, 18])

    def test_submitted_values_become_field_answers(self) -> None:
        components = [
            {"type": 18, "id": 1, "component": {"type": 3, "custom_id": "f0", "values": ["1"]}},
            {"type": 18, "id": 2, "component": {"type": 4, "custom_id": "f1", "value": "It crashes\n\nwhen I <@1> fly https://x.io [x](https://y.io)"}},
            {"type": 18, "id": 3, "component": {"type": 21, "custom_id": "f2", "value": "0"}},
            {"type": 18, "id": 4, "component": {"type": 4, "custom_id": "f3", "value": "   "}},
        ]
        answers = ai_asks.form_answers(FORM, components)
        self.assertEqual([label for label, _ in answers], ["Version", "What happens", "Launcher"])  # empty Name skipped
        self.assertEqual((answers[0][1], answers[2][1]), ("1.21", "CurseForge"))
        long_value = answers[1][1]
        self.assertNotIn("\n", long_value)
        self.assertNotIn("<@1>", long_value)
        self.assertNotIn("https://", long_value)
        self.assertNotIn("](", long_value)
        self.assertEqual(ai_asks.form_answers(FORM, [{"type": 18, "component": {"custom_id": "f0", "values": ["7"]}}]), [])
        self.assertEqual(ai_asks.form_answers(FORM, None), [])

    def test_answer_lines_fit_the_card_and_read_back(self) -> None:
        answers = [("Version", "1.21"), ("What happens", "word " * 400)]
        used = TEXT_LIMIT - 300
        lines = ai_asks.form_lines(42, answers, used=used)
        self.assertLessEqual(used + sum(map(len, lines)), TEXT_LIMIT)
        self.assertEqual(lines[0], "-# ↳ <@42> · Version: **1.21**")
        self.assertTrue(lines[1].endswith("…**"))
        user_id, content = ai_asks.read_answer(["What do you run?", *lines])
        self.assertEqual(user_id, 42)
        self.assertTrue(content.startswith("Version: 1.21\nWhat happens: word word"))

        self.assertEqual(ai_asks.read_answer(["Q?", ai_asks.pick_line(7, "Stats")]), (7, "Stats"))
        self.assertIsNone(ai_asks.read_answer(["Q?", "-# Bulma is AI"]))

    def test_static_text_follows_the_language(self) -> None:
        self.assertEqual(ai_asks.text("pt", "answered"), "Já respondido.")
        self.assertEqual(ai_asks.text("de", "answered"), "Already answered.")
        self.assertEqual(ai_asks.text(None, "fill_in"), "Fill in")
        self.assertEqual({key for texts in ai_asks.TEXT.values() for key in texts}, set(ai_asks.TEXT["en"]))


if __name__ == "__main__":
    unittest.main()
