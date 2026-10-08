import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from aiohttp.test_utils import TestClient, TestServer
from discord.components import _component_factory

from bulmaai.cogs.message_templates import MessageTemplatesCog
from bulmaai.services import message_templates
from bulmaai.services.cards import (
    AnnouncementError,
    build_card_view,
    card_from_message,
    count_components,
    language_row,
    normalize_card,
)
from bulmaai.services.message_templates import DEFAULT_TEMPLATES
from bulmaai.services.panel_announcements import translate_card
from bulmaai.web.core import SESSION_COOKIE, sign_session
from bulmaai.web.server import create_app
from test_admin_panel import HELPER_ID, OWNER_ID, SECRET, make_bot

CARD = {"accent_color": "#5865F2", "blocks": [{"type": "text", "text": "Hello"}]}
SECTION = {"type": "section", "text": "x", "thumbnail_url": "https://x.com/a.png"}
FULL_CARD = {
    "accent_color": "#F39C12",
    "blocks": [
        {"type": "text", "text": "## Title\n-# small"},
        {"type": "section", "text": "Side", "thumbnail_url": "https://x.com/a.png"},
        {"type": "separator", "divider": False, "spacing": "large"},
        {
            "type": "gallery",
            "images": [
                {"url": "https://x.com/b.png", "description": "alt"},
                {"url": "https://x.com/c.png", "description": None},
            ],
        },
        {
            "type": "buttons",
            "buttons": [
                {"label": "Patreon", "url": "https://www.patreon.com/DragonMineZ", "emoji": "🧡"},
                {"label": "Docs", "url": "https://x.com"},
            ],
        },
    ],
}


class NormalizeCardTests(unittest.TestCase):
    def test_cleans_and_uppercases_color(self):
        card = normalize_card({"accent_color": "f39c12", "blocks": [{"type": "text", "text": "  hi  "}]})
        self.assertEqual(card, {"accent_color": "#F39C12", "blocks": [{"type": "text", "text": "hi"}]})
        self.assertIsNone(normalize_card({"accent_color": None, "blocks": CARD["blocks"]})["accent_color"])

    def test_rejects_bad_cards(self):
        text = {"type": "text", "text": "x"}
        link = {"label": "a", "url": "https://x.com"}
        bad = [
            None,
            {"blocks": []},
            {"accent_color": "red", "blocks": [text]},
            {"blocks": [{"type": "text", "text": "  "}]},
            {"blocks": [{"type": "nope"}]},
            {"blocks": [dict(SECTION, thumbnail_url="javascript:alert(1)")]},
            {"blocks": [{"type": "separator", "spacing": "huge"}]},
            {"blocks": [{"type": "gallery", "images": []}]},
            {"blocks": [{"type": "gallery", "images": [{"url": "https://x.com/a.png"}] * 11}]},
            {"blocks": [{"type": "buttons", "buttons": [link] * 6}]},
            {"blocks": [{"type": "buttons", "buttons": [dict(link, label="x" * 81)]}]},
            {"blocks": [{"type": "buttons", "buttons": [dict(link, url="ftp://x.com")]}]},
            {"blocks": [{"type": "text", "text": "x" * 4001}]},
            {"blocks": [{"type": "text", "text": "x" * 3000}, {"type": "text", "text": "y" * 1001}]},
        ]
        for raw in bad:
            with self.assertRaises(AnnouncementError, msg=raw):
                normalize_card(raw)

    def test_component_limit(self):
        sections = [SECTION] * 13
        self.assertEqual(count_components({"blocks": sections}), 40)  # container + 13 sections of 3
        normalize_card({"blocks": sections})
        with self.assertRaisesRegex(AnnouncementError, "components"):
            normalize_card({"blocks": sections + [{"type": "text", "text": "x"}]})
        with self.assertRaisesRegex(AnnouncementError, "components"):
            normalize_card({"blocks": sections}, language_row=True)


class CardViewTests(unittest.IsolatedAsyncioTestCase):
    def message(self, card, rows=()):
        view = build_card_view(card, extra_rows=rows)
        return SimpleNamespace(components=[_component_factory(c) for c in view.to_components()])

    async def test_round_trip(self):
        card = normalize_card(FULL_CARD)
        self.assertEqual(card_from_message(self.message(card)), card)

    async def test_language_row_is_hidden_from_the_editor(self):
        message = self.message(normalize_card(CARD), rows=[language_row("rules")])
        self.assertEqual(card_from_message(message), normalize_card(CARD))

    async def test_unrepresentable_messages_raise(self):
        with self.assertRaises(AnnouncementError):
            card_from_message(SimpleNamespace(components=[]))
        row = discord.ui.ActionRow(discord.ui.Button(label="x", custom_id="other"))
        view = discord.ui.DesignerView(discord.ui.Container(discord.ui.TextDisplay("hi"), row))
        message = SimpleNamespace(components=[_component_factory(c) for c in view.to_components()])
        with self.assertRaisesRegex(AnnouncementError, "link buttons"):
            card_from_message(message)

    async def test_translate_card_translates_texts_labels_and_descriptions(self):
        settings = SimpleNamespace(announcement_role_en_id=1, announcement_role_es_id=2, announcement_role_pt_id=3)
        cog = SimpleNamespace(settings=settings)
        card = normalize_card(
            {
                "blocks": [
                    {"type": "text", "text": "hello <@&1>"},
                    {"type": "gallery", "images": [{"url": "https://x.com/a.png", "description": "alt"}]},
                    {"type": "buttons", "buttons": [{"label": "Go", "url": "https://x.com"}]},
                ]
            }
        )
        fake = AsyncMock(side_effect=lambda _cog, text, lang: f"{lang}:{text}")
        with patch("bulmaai.services.panel_announcements.translate_text", fake):
            out = await translate_card(cog, card, "es")
        self.assertEqual(out["blocks"][0]["text"], "es:hello <@&2>")
        self.assertEqual(out["blocks"][1]["images"][0], {"url": "https://x.com/a.png", "description": "es:alt"})
        self.assertEqual(out["blocks"][2]["buttons"][0], {"label": "es:Go", "url": "https://x.com"})
        self.assertEqual(card["blocks"][0]["text"], "hello <@&1>")


class LanguageButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = patch("bulmaai.services.message_templates._templates_path", return_value=Path(tmp.name) / "t.json")
        path.start()
        self.addCleanup(path.stop)
        self.cog = MessageTemplatesCog(SimpleNamespace())

    def interaction(self, custom_id, *, ephemeral=False):
        return SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": custom_id},
            message=SimpleNamespace(flags=SimpleNamespace(ephemeral=ephemeral)),
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: False),
        )

    def first_text(self, kwargs):
        return kwargs["view"].to_components()[0]["components"][0]["content"]

    async def test_click_sends_private_copy_in_that_language(self):
        interaction = self.interaction("tpl_lang:rules:es")
        await self.cog.on_interaction(interaction)
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(self.first_text(kwargs), DEFAULT_TEMPLATES["rules"]["languages"]["es"]["blocks"][0]["text"])
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        row = kwargs["view"].to_components()[0]["components"][-1]
        self.assertEqual(row["components"][2]["custom_id"], "tpl_lang:rules:pt")

    async def test_click_inside_private_copy_edits_in_place(self):
        interaction = self.interaction("tpl_lang:rules:pt", ephemeral=True)
        await self.cog.on_interaction(interaction)
        interaction.response.send_message.assert_not_awaited()
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertEqual(self.first_text(kwargs), DEFAULT_TEMPLATES["rules"]["languages"]["pt"]["blocks"][0]["text"])

    async def test_missing_language_falls_back_to_english_and_unknown_template_is_outdated(self):
        message_templates.create_template({"name": "News", "language_buttons": True, "languages": {"en": CARD}})
        interaction = self.interaction("tpl_lang:news:es")
        await self.cog.on_interaction(interaction)
        self.assertEqual(self.first_text(interaction.response.send_message.await_args.kwargs), "Hello")

        gone = self.interaction("tpl_lang:gone:es")
        await self.cog.on_interaction(gone)
        self.assertEqual(gone.response.send_message.await_args.args[0], "This message is outdated.")
        self.assertTrue(gone.response.send_message.await_args.kwargs["ephemeral"])

    async def test_ignores_other_buttons(self):
        for custom_id in ("modcase:edit:1", "tpl_lang:rules:fr", "tpl_lang:rules"):
            interaction = self.interaction(custom_id)
            await self.cog.on_interaction(interaction)
            interaction.response.send_message.assert_not_awaited()


class TemplatesPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = make_bot()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "message_templates.json"
        for target, kwargs in (
            ("bulmaai.services.message_templates._templates_path", {"return_value": self.file}),
            ("bulmaai.web.core.get_pool", {"new": AsyncMock(return_value=None)}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.audit = patch("bulmaai.web.routes_templates.audit", AsyncMock()).start()
        self.addCleanup(patch.stopall)

        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.origin = {"Origin": f"http://{self.client.host}:{self.client.port}"}
        self.login(OWNER_ID)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self, user_id):
        self.client.session.cookie_jar.update_cookies({SESSION_COOKIE: sign_session(SECRET, user_id)})

    async def write(self, method, path, body):
        return await self.client.request(method, path, json=body, headers=self.origin)

    async def test_helper_is_denied(self):
        self.login(HELPER_ID)
        self.assertEqual((await self.client.get("/api/templates")).status, 403)

    async def test_list_has_builtin_defaults(self):
        templates = (await (await self.client.get("/api/templates")).json())["templates"]
        self.assertEqual([t["id"] for t in templates], ["rules", "support"])
        rules = templates[0]
        self.assertTrue(rules["builtin"] and rules["language_buttons"])
        self.assertFalse(rules["customized"])
        self.assertEqual(set(rules["languages"]), {"en", "es", "pt"})
        for template in templates:
            for card in template["languages"].values():
                self.assertEqual(normalize_card(card, language_row=True), card)

    async def test_create_update_delete_custom_template(self):
        body = {"name": "Big News!", "language_buttons": False, "languages": {"en": CARD, "es": None, "pt": None}}
        created = await self.write("POST", "/api/templates", body)
        self.assertEqual(created.status, 200, await created.text())
        template = await created.json()
        self.assertEqual((template["id"], template["builtin"], template["customized"]), ("big-news", False, False))
        self.assertEqual((await (await self.write("POST", "/api/templates", body)).json())["id"], "big-news-2")

        updated = await self.write("PUT", "/api/templates/big-news", dict(body, name="Renamed", language_buttons=True))
        self.assertEqual((await updated.json())["name"], "Renamed")
        self.assertEqual(self.audit.await_args.args[1:3], ("templates.update", "big-news"))

        deleted = await self.write("DELETE", "/api/templates/big-news", None)
        self.assertEqual(await deleted.json(), {"template": None})
        self.assertEqual((await self.write("DELETE", "/api/templates/big-news", None)).status, 404)
        self.assertEqual((await self.write("PUT", "/api/templates/big-news", body)).status, 404)

    async def test_builtin_customize_and_reset(self):
        body = {"name": "Rules", "language_buttons": True, "languages": {"en": CARD, "es": None, "pt": None}}
        saved = await (await self.write("PUT", "/api/templates/rules", body)).json()
        self.assertTrue(saved["customized"])
        stored = json.loads(self.file.read_text(encoding="utf-8"))["templates"]["rules"]
        self.assertEqual(stored["languages"]["en"], CARD)

        reset = await (await self.write("DELETE", "/api/templates/rules", None)).json()
        self.assertFalse(reset["template"]["customized"])
        self.assertEqual(reset["template"]["languages"]["en"], DEFAULT_TEMPLATES["rules"]["languages"]["en"])
        self.assertEqual(self.audit.await_args.args[1:3], ("templates.reset", "rules"))

    async def test_validation_errors(self):
        good = {"name": "X", "language_buttons": False, "languages": {"en": CARD}}
        for body in (
            dict(good, name=" "),
            dict(good, languages={"es": CARD}),
            dict(good, languages={"en": {"blocks": []}}),
            dict(good, language_buttons="yes"),
        ):
            response = await self.write("POST", "/api/templates", body)
            self.assertEqual(response.status, 400, body)
            self.assertIn("error", await response.json())
        # 13 sections fit in 40 components, but not together with the language buttons.
        big = dict(good, languages={"en": {"blocks": [SECTION] * 13}})
        self.assertEqual((await self.write("POST", "/api/templates", big)).status, 200)
        self.assertEqual((await self.write("POST", "/api/templates", dict(big, language_buttons=True))).status, 400)

    async def test_post_sends_english_card_with_language_row(self):
        message = SimpleNamespace(id=9, jump_url="https://discord.com/x")
        channel = SimpleNamespace(id=5, send=AsyncMock(return_value=message))
        with (
            patch("bulmaai.web.routes_templates.resolve_channel", return_value=channel),
            patch("bulmaai.web.routes_templates.check_bot_can"),
        ):
            response = await self.write("POST", "/api/templates/support/post", {"channel_id": "5"})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await self.write("POST", "/api/templates/nope/post", {"channel_id": "5"})).status, 404)
            self.login(HELPER_ID)
            self.assertEqual((await self.write("POST", "/api/templates/rules/post", {"channel_id": "5"})).status, 403)
        kwargs = channel.send.await_args.kwargs
        self.assertEqual(set(kwargs), {"view", "allowed_mentions"})
        container = kwargs["view"].to_components()[0]
        self.assertEqual(container["components"][-1]["components"][0]["custom_id"], "tpl_lang:support:en")
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertEqual(self.audit.await_args.args[1:3], ("templates.post", "support"))


if __name__ == "__main__":
    unittest.main()
