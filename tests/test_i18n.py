import importlib
import pkgutil
import string
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bulmaai.cogs
import bulmaai.ui
from bulmaai.utils import i18n
from bulmaai.utils.i18n import described, t, user_language

from v2_helpers import buttons, texts

for _package in (bulmaai.cogs, bulmaai.ui):
    for _module in pkgutil.iter_modules(_package.__path__):
        importlib.import_module(f"{_package.__name__}.{_module.name}")
importlib.import_module("bulmaai.services.mod_actions")

SETTINGS = SimpleNamespace(
    panel_guild_id=1, announcement_role_en_id=10, announcement_role_es_id=20, announcement_role_pt_id=30
)
MAIN = SimpleNamespace(id=1)


def member(*role_ids, guild=MAIN, user_id=5):
    return SimpleNamespace(id=user_id, guild=guild, roles=[SimpleNamespace(id=r) for r in role_ids])


def interaction(user, locale=None, client=None):
    return SimpleNamespace(user=user, locale=locale, client=client)


class DecisionOrderTests(unittest.TestCase):
    def test_single_role_beats_locale(self):
        self.assertEqual(user_language(member(20), SETTINGS, interaction(None, "pt-BR")), "es")

    def test_several_roles_use_locale_when_it_is_one_of_them(self):
        self.assertEqual(user_language(member(10, 30), SETTINGS, interaction(None, "pt-BR")), "pt")

    def test_several_roles_without_matching_locale_take_the_first(self):
        self.assertEqual(user_language(member(20, 30), SETTINGS, interaction(None, "en-US")), "es")
        self.assertEqual(user_language(member(20, 30), SETTINGS), "es")

    def test_no_role_uses_a_supported_locale(self):
        self.assertEqual(user_language(member(), SETTINGS, interaction(None, "es-419")), "es")
        self.assertEqual(user_language(member(), SETTINGS, interaction(None, "es-ES")), "es")
        self.assertEqual(user_language(member(), SETTINGS, interaction(None, "pt-BR")), "pt")

    def test_no_role_and_unsupported_locale_is_english(self):
        self.assertEqual(user_language(member(), SETTINGS, interaction(None, "fr")), "en")
        self.assertEqual(user_language(member(), SETTINGS), "en")

    def test_dm_reads_roles_off_the_main_server_member(self):
        dm_user = SimpleNamespace(id=5)
        main = SimpleNamespace(get_member=lambda user_id: member(30) if user_id == 5 else None)
        client = SimpleNamespace(get_guild=lambda guild_id: main if guild_id == 1 else None)
        self.assertEqual(user_language(dm_user, SETTINGS, interaction(dm_user, "es-419", client)), "pt")

    def test_reader_language_reads_the_interaction(self):
        self.assertEqual(i18n.reader_language(interaction(member(20), "pt-BR"), SETTINGS), "es")


class TableTests(unittest.TestCase):
    def test_t_falls_back_to_english(self):
        texts_ = {"en": {"hi": "Hi {name}", "only": "English"}, "es": {"hi": "Hola {name}"}}
        self.assertEqual(t(texts_, "hi", "es", name="Ana"), "Hola Ana")
        self.assertEqual(t(texts_, "only", "es"), "English")
        self.assertEqual(t(texts_, "hi", "de", name="Ana"), "Hi Ana")

    def test_every_table_has_every_key_in_every_language(self):
        self.assertGreater(len(i18n.TABLES), 20)
        for table in i18n.TABLES:
            for language in i18n.LANGUAGES:
                self.assertIn(language, table)
                self.assertEqual(set(table[language]), set(table["en"]), f"{language}: {sorted(table['en'])[:3]}")

    def test_placeholders_match_english(self):
        def fields(value):
            if not isinstance(value, str):
                return set()
            return {name for _, name, _, _ in string.Formatter().parse(value) if name}

        for table in i18n.TABLES:
            for key, english in table["en"].items():
                for language in i18n.LANGUAGES:
                    self.assertEqual(fields(table[language][key]), fields(english), f"{language}.{key}")

    def test_descriptions_fit_discords_limit(self):
        from bulmaai.cogs import mod_commands

        for language in i18n.LANGUAGES:
            for text in mod_commands.DESC[language].values():
                self.assertLessEqual(len(text), 100)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def test_commands_carry_description_localizations(self):
        from bulmaai.cogs.mod_commands import ModCommandsCog
        from bulmaai.cogs.patreon_whitelist_flow import PatreonWhitelistFlowCog

        for command in (ModCommandsCog.warn, PatreonWhitelistFlowCog.patreon):
            self.assertEqual(set(command.description_localizations), {"es-ES", "es-419", "pt-BR"})
        self.assertNotEqual(ModCommandsCog.warn.description_localizations["pt-BR"], ModCommandsCog.warn.description)
        self.assertEqual(described({"en": {"k": "E"}, "es": {"k": "S"}, "pt": {"k": "P"}}, "k"), {
            "description": "E",
            "description_localizations": {"es-ES": "S", "es-419": "S", "pt-BR": "P"},
        })

    async def test_ephemeral_error_follows_the_locale(self):
        from bulmaai.cogs.power_level import PowerLevelCog

        ctx = SimpleNamespace(guild=None, user=None, locale="es-419", respond=AsyncMock())
        await PowerLevelCog.powerlevel.callback(object.__new__(PowerLevelCog), ctx)
        ctx.respond.assert_awaited_once_with("Este comando solo funciona en un servidor.", ephemeral=True)

    def test_mod_dm_and_appeal_label_in_the_targets_language(self):
        from bulmaai.services import mod_actions

        text = mod_actions.dm_text("DMZ", "warn", "", None, "es")
        self.assertIn("Recibiste una advertencia en **DMZ**.", text)
        self.assertEqual(mod_actions.ModActionError("self").text("pt"), t(mod_actions.ERRORS, "self", "pt"))
        self.assertNotEqual(t(mod_actions.DM, "appeal", "pt"), t(mod_actions.DM, "appeal", "en"))

    async def test_confirm_prompt_in_portuguese(self):
        from bulmaai.cogs.mod_commands import TEXT, ConfirmView

        view = ConfirmView(t(TEXT, "clear_prompt", "pt", user_id=1), t(TEXT, "clear_button", "pt"), AsyncMock(), "pt")
        self.assertIn(t(TEXT, "clear_prompt", "pt", user_id=1), texts(view)[0])
        self.assertIn(t(TEXT, "cancel", "pt"), [button.label for button in buttons(view)])
