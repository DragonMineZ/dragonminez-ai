import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.ui import ai_asks
from bulmaai.ui.v2 import TEXT_LIMIT
from v2_helpers import texts, walk

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
        self.assertIn("kind must be", validate(kind="video")["error"])
        self.assertIn("kind must be", validate(kind="action", action="get_staff")["error"])  # offer_action's own kind
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
        self.assertEqual(ai_asks.ask_rows({"kind": "video"}, 42, "en"), [])
        self.assertEqual(ai_asks.ask_rows({"kind": "action", "action": "nope"}, 42, "en"), [])

    def test_custom_ids_round_trip(self) -> None:
        for kind in ai_asks.CONTROL_KINDS:
            value = ai_asks.custom_id(kind, 123456789012345678, 15)
            self.assertLessEqual(len(value), 100)
            self.assertEqual(ai_asks.parse_custom_id(value), (kind, 123456789012345678, 15))
        for bad in ("ai_ask:video:42:0", "ai_ask:choices:x:0", "ai_ask:choices:42", "ticket_resolved:yes:42:en"):
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

    def test_upload_lines_are_not_answers(self) -> None:
        lines = [ai_asks.upload_line(42, "latest.log"), ai_asks.upload_line(42, "shot.png")]
        self.assertEqual(lines[0], "-# ↳ <@42> uploaded **latest.log**")
        self.assertIsNone(ai_asks.read_answer(["Send your log", *lines]))
        self.assertEqual(ai_asks.read_upload(["Send your log", *lines]), (42, ["latest.log", "shot.png"]))
        self.assertIsNone(ai_asks.read_upload(["Q?", ai_asks.pick_line(42, "Stats")]))

    def test_static_text_follows_the_language(self) -> None:
        self.assertEqual(ai_asks.text("pt", "answered"), "Já respondido.")
        self.assertEqual(ai_asks.text("de", "answered"), "Already answered.")
        self.assertEqual(ai_asks.text(None, "fill_in"), "Fill in")
        self.assertEqual({key for texts in ai_asks.TEXT.values() for key in texts}, set(ai_asks.TEXT["en"]))


LOG_ASK = {"kind": "file", "what": "log", "max_files": 2, "label": "Upload latest.log"}


class FileTests(unittest.IsolatedAsyncioTestCase):
    def test_file_spec_defaults_and_limits(self) -> None:
        self.assertEqual(validate(kind="file")["shown"], {"kind": "file", "what": "any", "max_files": 1})
        out = validate(kind="file", what="log", label="Upload latest.log", max_files=3)
        self.assertEqual(out["shown"], {"kind": "file", "what": "log", "max_files": 3, "label": "Upload latest.log"})
        self.assertNotIn("dropped", out)  # "latest.log" only looks like a link: allowed in a label
        for bad in (0, 4, "2", True, 1.5):
            out = validate(kind="file", what="screenshot", max_files=bad)
            self.assertEqual(out["shown"]["max_files"], 1, bad)
            self.assertIn("max_files", out["dropped"][0])
        out = validate(kind="file", label="see https://evil.example/x")
        self.assertNotIn("label", out["shown"])
        self.assertIn("label", out["dropped"][0])
        self.assertIn("what", validate(kind="file", what="video")["error"])

    async def test_log_card_has_upload_and_where_rows(self) -> None:
        upload_row, where_row = ai_asks.ask_rows(LOG_ASK, 42, "es")
        (upload,) = upload_row.children
        self.assertEqual(
            (upload.label, str(upload.emoji), upload.custom_id, upload.style),
            ("Upload latest.log", "📎", "ai_ask:file:42:0", discord.ButtonStyle.secondary),
        )
        (where,) = where_row.children
        self.assertEqual((where.custom_id, where.placeholder), ("ai_ask:file:42:1", "¿Dónde está?"))
        self.assertEqual([o.value for o in where.options], ["windows", "mac", "linux", "server", "launchers"])
        self.assertEqual(where.options[0].label, "🪟 Windows (Cliente)")
        self.assertEqual(ai_asks.parse_custom_id(where.custom_id), ("file", 42, ai_asks.WHERE_INDEX))

        (row,) = ai_asks.ask_rows({"kind": "file", "what": "screenshot", "max_files": 1}, 42, "pt")
        (button,) = row.children
        self.assertEqual(button.label, "Enviar")  # no "Where is it?" for a screenshot

    async def test_where_card_shows_the_picked_place(self) -> None:
        windows = "\n".join(texts(ai_asks.where_card("en", ["windows"])))
        self.assertIn("**🪟 Windows (Client)**", windows)
        self.assertIn("%APPDATA%\\.minecraft\\logs\\latest.log", windows)
        launchers = "\n".join(texts(ai_asks.where_card("pt", ["launchers"])))
        self.assertIn("CurseForge", launchers)
        self.assertIn("clique com o botão direito", launchers)
        self.assertIsNone(ai_asks.where_card("en", ["nope"]))
        self.assertIsNone(ai_asks.where_card("en", None))

    async def test_upload_modal_wraps_one_file_field(self) -> None:
        modal = ai_asks.file_modal(LOG_ASK, 55, "en")
        self.assertEqual((modal.custom_id, modal.title, modal._store), ("ai_ask_file:55", "Upload your log", False))
        (label,) = modal.children
        self.assertEqual(label.label, "latest.log or crash-report.txt")
        self.assertIsInstance(label.item, discord.ui.FileUpload)
        self.assertEqual((label.item.custom_id, label.item.min_values, label.item.max_values, label.item.required), ("files", 1, 2, True))
        self.assertEqual(modal.to_dict()["components"][0]["component"]["type"], 19)
        self.assertEqual(ai_asks.file_modal({"kind": "file", "what": "screenshot"}, 55, "es").title, "Sube una captura")
        self.assertEqual(ai_asks.parse_file_modal_id(ai_asks.file_modal_id(55)), 55)
        self.assertIsNone(ai_asks.parse_file_modal_id(ai_asks.form_modal_id(55)))
        self.assertIsNone(ai_asks.parse_form_modal_id(ai_asks.file_modal_id(55)))

    def test_submitted_attachment_ids_resolve_to_their_payloads(self) -> None:
        log = {"id": "11", "filename": "latest.log", "size": 10, "url": "https://cdn/11"}
        shot = {"id": "12", "filename": "shot.png", "size": 20, "url": "https://cdn/12"}
        data = {
            "custom_id": "ai_ask_file:55",
            "components": [{"type": 18, "component": {"type": 19, "custom_id": "files", "values": ["11", "99", "12"]}}],
            "resolved": {"attachments": {"11": log, "12": shot}},
        }
        self.assertEqual(ai_asks.uploaded(LOG_ASK, data), [log, shot])  # an unresolved id is skipped
        self.assertEqual(ai_asks.uploaded({"kind": "file"}, data), [log])  # at most max_files
        self.assertEqual(ai_asks.uploaded(LOG_ASK, {**data, "resolved": None}), [])
        self.assertEqual(ai_asks.uploaded(LOG_ASK, {"components": None}), [])
        self.assertEqual(ai_asks.uploaded(LOG_ASK, None), [])

    def test_upload_names_are_plain_short_and_distinct(self) -> None:
        names = ai_asks.upload_names(["latest.log", "latest.log", "my crash <@1> https://x.io.txt", "", "a" * 90 + ".log"])
        self.assertEqual(names[:4], ["latest.log", "1_latest.log", "my_crash___1__https___x.io.txt", "file"])
        self.assertEqual((len(names[4]), names[4][-4:]), (ai_asks.UPLOAD_NAME_LIMIT, ".log"))
        self.assertEqual(ai_asks.upload_names(["2_x", "x", "x", "x"]), ["2_x", "x", "1_x", "3_x"])

    async def test_upload_card_credits_the_requester_and_carries_the_files(self) -> None:
        view = ai_asks.upload_card(42, ["latest.log", "shot.png"], [False, True], discord.Color.blue())
        self.assertEqual(texts(view), ["-# ↳ <@42> uploaded **latest.log**", "-# ↳ <@42> uploaded **shot.png**"])
        (gallery,) = [item for item in walk(view) if isinstance(item, discord.ui.MediaGallery)]
        self.assertEqual([item.url for item in gallery.items], ["attachment://shot.png"])
        (file,) = [item for item in walk(view) if isinstance(item, discord.ui.File)]
        self.assertEqual(file.url, "attachment://latest.log")
        self.assertEqual(ai_asks.read_upload(texts(view)), (42, ["latest.log", "shot.png"]))


STEPS = {"kind": "steps", "steps": ["Close Minecraft", "Delete the config folder", "Start the game"]}


class StepsTests(unittest.IsolatedAsyncioTestCase):
    def test_steps_keep_up_to_eight_distinct_screened_steps(self) -> None:
        out = validate(kind="steps", steps=["Close Minecraft", "close minecraft", "<@1>", "x" * 201, *(f"Step {n}" for n in range(9))])
        self.assertEqual(out["shown"], {"kind": "steps", "steps": ["Close Minecraft", *(f"Step {n}" for n in range(7))]})
        self.assertEqual(len(out["dropped"]), 4)  # duplicate, ping, too long, the steps after the eighth
        self.assertIn("don't list them again", out["note"])
        self.assertIn("2-8", validate(kind="steps", steps=["Only one"])["error"])
        self.assertIn("2-8", validate(kind="steps", steps="Close it, then open it")["error"])

    async def test_the_card_shows_the_list_and_the_current_steps_buttons(self) -> None:
        text, row = ai_asks.ask_rows(STEPS, 42, "es")
        self.assertIsInstance(text, discord.ui.TextDisplay)
        self.assertEqual(text.id, ai_asks.CONTROLS_TEXT_ID)
        self.assertEqual(text.content, "**1\\. Close Minecraft**\n2\\. Delete the config folder\n3\\. Start the game")
        self.assertEqual(
            [(b.label, str(b.emoji), b.custom_id) for b in row.children],
            [("Hecho", "✅", "ai_ask:steps:42:0"), ("Sigue sin funcionar", "❌", "ai_ask:steps:42:1")],
        )

        text, row = ai_asks.steps_items(STEPS, 42, "pt", progress=2)
        self.assertEqual(text.content, "✅ 1\\. Close Minecraft\n✅ 2\\. Delete the config folder\n**3\\. Start the game**")
        self.assertEqual([(b.label, b.custom_id) for b in row.children], [("Feito", "ai_ask:steps:42:4"), ("Ainda não funciona", "ai_ask:steps:42:5")])
        self.assertEqual([ai_asks.split_step_index(ai_asks.parse_custom_id(b.custom_id)[2]) for b in row.children], [(2, 0), (2, 1)])

    def test_final_lists(self) -> None:
        self.assertEqual(
            ai_asks.steps_text(STEPS, 1, failed=1),
            "✅ 1\\. Close Minecraft\n❌ 2\\. Delete the config folder\n3\\. Start the game",
        )
        self.assertEqual(ai_asks.steps_text(STEPS, 3).count("✅"), 3)
        self.assertNotIn("**", ai_asks.steps_text(STEPS, 3))

    def test_outcome_lines_read_back_in_english_in_every_language(self) -> None:
        self.assertEqual(
            ai_asks.step_failed_line(42, 1, "Delete the config folder", "en"),
            "-# ↳ <@42>: **Step 2 didn't work:** Delete the config folder",
        )
        self.assertEqual(ai_asks.steps_done_line(42, 3, "en"), "-# ↳ <@42>: **Did all the steps (1-3)**")
        for language in ("en", "es", "pt"):
            failed = ai_asks.step_failed_line(42, 1, "Delete **config**", language)
            self.assertEqual(ai_asks.read_answer(["Try these", failed]), (42, "Step 2 didn't work: Delete **config**"))
            done = ai_asks.steps_done_line(42, 3, language)
            self.assertEqual(ai_asks.read_answer(["Try these", done]), (42, "I did all the steps (1-3)."))
        self.assertIn("El paso 2 no funcionó:", ai_asks.step_failed_line(42, 1, "x", "es"))
        self.assertEqual(ai_asks.read_answer([ai_asks.pick_line(42, "Step 2 didn't work:")]), (42, "Step 2 didn't work:"))

    async def test_progress_reads_back_off_the_buttons(self) -> None:
        def ids(progress):
            return [b.custom_id for b in ai_asks.steps_items(STEPS, 42, "en", progress)[1].children]

        self.assertIsNone(ai_asks.read_progress(ids(0)))
        self.assertEqual(ai_asks.read_progress([None, *ids(1)]), (42, "I did step 1."))
        self.assertEqual(ai_asks.read_progress(ids(2)), (42, "I did steps 1-2."))
        self.assertIsNone(ai_asks.read_progress(["ai_ask:choices:42:3", "ticket_btn_close"]))

    def test_room_for_the_steps_and_where_controls_may_go(self) -> None:
        self.assertEqual(ai_asks.reserved_text(None), 0)
        self.assertEqual(ai_asks.reserved_text({"kind": "choices", "choices": ["a", "b"]}), 0)
        longest = max(len(ai_asks.steps_text(STEPS, p)) for p in range(4))
        line = ai_asks.step_failed_line(123456789012345678, 7, "x" * ai_asks.STEP_LIMIT, "pt")
        eight = {"kind": "steps", "steps": ["x" * ai_asks.STEP_LIMIT] * 8}
        self.assertGreaterEqual(ai_asks.reserved_text(STEPS), longest + len(line) - ai_asks.STEP_LIMIT)
        self.assertGreaterEqual(ai_asks.reserved_text(eight), len(ai_asks.steps_text(eight, 0)) + len(line))

        self.assertFalse(ai_asks.allowed_in({"kind": "file"}, "public"))
        self.assertTrue(ai_asks.allowed_in({"kind": "file"}, "dm"))
        self.assertTrue(ai_asks.allowed_in(STEPS, "public"))
        self.assertTrue(ai_asks.allowed_in({"kind": "action", "action": "get_staff"}, "ticket"))
        self.assertFalse(ai_asks.allowed_in({"kind": "action", "action": "get_staff"}, "dm"))
        self.assertFalse(ai_asks.allowed_in({"kind": "action", "action": "open_ticket"}, "ticket"))
        self.assertFalse(ai_asks.allowed_in({"kind": "action", "action": "open_ticket"}, "dm"))
        self.assertTrue(ai_asks.allowed_in({"kind": "action", "action": "open_ticket"}, "public"))
        self.assertFalse(ai_asks.allowed_in({"kind": "action", "action": "nope"}, "public"))


def offer(channel_kind=None, **args):
    return ai_asks.validate_action(args, SETTINGS, channel_kind)


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def test_actions_only_where_they_can_run(self) -> None:
        self.assertIn("action must be", offer(action="ban")["error"])
        self.assertIn("in tickets", offer("dm", action="get_staff")["error"])
        self.assertIn("in public channels", offer("ticket", action="open_ticket")["error"])
        self.assertFalse(offer("dm", action="open_ticket")["ok"])
        for where in ("ticket", "dm", "public", None):
            self.assertEqual(offer(where, action="link_patreon")["shown"], {"kind": "action", "action": "link_patreon"})
        self.assertEqual(offer(action="get_staff")["shown"]["action"], "get_staff")  # unknown channel: the cog checks
        self.assertIn("runs only when they click", offer("ticket", action="get_staff")["note"])

    def test_label_category_and_no_thanks(self) -> None:
        out = offer("public", action="open_ticket", label="Abrir ticket", category="bug", no_thanks=True)
        self.assertEqual(
            out["shown"], {"kind": "action", "action": "open_ticket", "label": "Abrir ticket", "category": "bug", "no_thanks": True}
        )
        self.assertNotIn("dropped", out)
        out = offer("public", action="open_ticket", label="see https://evil.example/x", category="refunds", no_thanks="yes")
        self.assertEqual(out["shown"], {"kind": "action", "action": "open_ticket"})
        self.assertEqual(len(out["dropped"]), 2)
        out = offer("ticket", action="get_staff", category="bug")
        self.assertEqual(out["shown"], {"kind": "action", "action": "get_staff"})
        self.assertIn("only for open_ticket", out["dropped"][0])

    async def test_one_primary_button_and_an_optional_no_thanks(self) -> None:
        (row,) = ai_asks.ask_rows({"kind": "action", "action": "get_staff"}, 42, "es")
        (button,) = row.children
        self.assertEqual((button.label, button.style, button.custom_id), ("Llamar al staff", discord.ButtonStyle.primary, "ai_ask:action:42:0"))
        (row,) = ai_asks.ask_rows({"kind": "action", "action": "link_patreon", "label": "Link it", "no_thanks": True}, 42, "pt")
        self.assertEqual(
            [(b.label, b.style, b.custom_id) for b in row.children],
            [("Link it", discord.ButtonStyle.primary, "ai_ask:action:42:0"), ("Não, obrigado", discord.ButtonStyle.secondary, "ai_ask:action:42:1")],
        )
        self.assertEqual(ai_asks.action_label({"action": "open_ticket"}, "en"), "Open a ticket")


BUG = {
    "kind": "action",
    "action": "report_bug",
    "title": "Crash when transforming",
    "what_happened": "The game closes\nwhen I go Super Saiyan",
    "steps": "1. Load a world\n2. Transform",
    "version": "DragonMineZ 2.1.3, Minecraft 1.20.1",
}


def _bug_submit(**values):
    return [{"type": 18, "component": {"type": 4, "custom_id": key, "value": value}} for key, value in values.items()]


class ReportBugTests(unittest.IsolatedAsyncioTestCase):
    def test_only_outside_tickets(self) -> None:
        self.assertIn("in public channels and DMs", offer("ticket", action="report_bug")["error"])
        for where in ("public", "dm"):
            self.assertEqual(offer(where, action="report_bug")["shown"], {"kind": "action", "action": "report_bug"})
        self.assertFalse(ai_asks.allowed_in({"kind": "action", "action": "report_bug"}, "ticket"))
        self.assertTrue(ai_asks.allowed_in({"kind": "action", "action": "report_bug"}, "dm"))
        self.assertTrue(ai_asks.allowed_in({"kind": "action", "action": "report_bug"}, "public"))

    def test_prefill_is_screened_and_keeps_the_long_fields_lines(self) -> None:
        out = offer(
            "public",
            action="report_bug",
            title="  Crash   when transforming ",
            what_happened="The game closes  \n  when I go Super Saiyan",
            steps="1. Load a world\n2. Transform",
            version="DragonMineZ 2.1.3, Minecraft 1.20.1",
        )
        self.assertEqual(out["shown"], BUG)
        self.assertNotIn("dropped", out)

        out = offer(
            "public",
            action="report_bug",
            title="x" * 101,
            what_happened="Get the fix at https://evil.example/fix.jar",
            steps="ping <@1> and @everyone",
            version="slurs everywhere",
        )
        self.assertEqual(out["shown"], {"kind": "action", "action": "report_bug"})
        self.assertEqual(len(out["dropped"]), 4)
        out = offer("public", action="report_bug", what_happened="x" * 1001, steps=["not", "text"])
        self.assertEqual(out["shown"], {"kind": "action", "action": "report_bug"})
        self.assertIn("over 1000 characters", out["dropped"][0])

        out = offer("public", action="open_ticket", title="Crash", steps="1. Load")
        self.assertEqual(out["shown"], {"kind": "action", "action": "open_ticket"})
        self.assertEqual(out["dropped"], ["title: only for report_bug", "steps: only for report_bug"])

    async def test_the_button_and_its_prefilled_form(self) -> None:
        (row,) = ai_asks.ask_rows(BUG, 42, "es")
        (button,) = row.children
        self.assertEqual((button.label, button.custom_id), ("Reportar este bug", "ai_ask:action:42:0"))

        modal = ai_asks.bug_modal(BUG, 55, "es")
        self.assertEqual((modal.custom_id, modal.title, modal._store), ("ai_ask_bug:55", "Reportar un bug", False))
        self.assertEqual(ai_asks.parse_bug_modal_id(modal.custom_id), 55)
        self.assertIsNone(ai_asks.parse_bug_modal_id("ai_ask_form:55"))
        self.assertEqual([child.label for child in modal.children], ["Título", "Qué pasó", "Pasos para reproducirlo", "Versión"])
        self.assertEqual(
            [(c.item.custom_id, c.item.style, c.item.required, c.item.max_length, c.item.value) for c in modal.children],
            [
                ("title", discord.InputTextStyle.short, True, 100, BUG["title"]),
                ("what_happened", discord.InputTextStyle.long, True, 1000, BUG["what_happened"]),
                ("steps", discord.InputTextStyle.long, False, 1000, BUG["steps"]),
                ("version", discord.InputTextStyle.short, False, 100, BUG["version"]),
            ],
        )
        self.assertEqual([part["type"] for part in modal.to_dict()["components"]], [18, 18, 18, 18])
        empty = ai_asks.bug_modal({"kind": "action", "action": "report_bug"}, 55, "en")
        self.assertEqual([c.item.value for c in empty.children], [None, None, None, None])
        self.assertEqual(empty.title, "Report a bug")

    def test_submitted_fields(self) -> None:
        answers = ai_asks.bug_answers(
            _bug_submit(title="  Crash \n on load ", what_happened=" It crashes\n\nat start ", steps="  ", version="2.1.3")
        )
        self.assertEqual(answers, {"title": "Crash on load", "what_happened": "It crashes\n\nat start", "version": "2.1.3"})
        self.assertEqual(ai_asks.bug_answers(None), {})

    def test_the_post_credits_the_reporter_and_is_safe_to_repost(self) -> None:
        body = ai_asks.bug_post(
            42, {"title": "Crash", "what_happened": "Boom <@1> @everyone [hotfix](https://x.io)", "version": "2.1.3"}
        )
        self.assertTrue(body.startswith("Reported by <@42> with Bulma\n\n**What happened**\nBoom "))
        self.assertTrue(body.endswith("\n\n**Version**\n2.1.3"))
        self.assertNotIn("**Steps**", body)
        self.assertNotIn("<@1>", body)
        self.assertNotIn("@everyone", body)
        self.assertNotIn("](", body)
        self.assertNotIn("https://", body)
        self.assertNotIn("Crash", body)  # the title is the post's name

        long = ai_asks.bug_post(42, {"what_happened": "word " * 300, "steps": "step " * 200, "version": "2.1.3"})
        self.assertLessEqual(len(long), ai_asks.BUG_POST_LIMIT)
        self.assertIn("**Steps**\nstep", long)
        self.assertTrue(long.endswith("**Version**\n2.1.3"))

    def test_the_outcome_line_reads_back_without_the_link(self) -> None:
        link = "https://discord.com/channels/1/777"
        line = ai_asks.bug_line(42, link, "en")
        self.assertEqual(line, f"-# ↳ <@42>: **Reported the bug** → {link}")
        for language in ai_asks.TEXT:
            self.assertEqual(
                ai_asks.read_answer(["This looks like a bug.", ai_asks.bug_line(42, link, language)]),
                (42, "I reported the bug in the bug-report forum."),
            )
        self.assertEqual(ai_asks.read_answer(["Q?", ai_asks.pick_line(7, "Reported the bug")]), (7, "Reported the bug"))


if __name__ == "__main__":
    unittest.main()
