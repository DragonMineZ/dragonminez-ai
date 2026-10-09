import os

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.config import get_editable_setting_names
from bulmaai.web.routes_settings import _owner_only
from bulmaai.web.settings_meta import META, SECTIONS, describe

SECTION_IDS = {section_id for section_id, _title, _description in SECTIONS}


def test_every_editable_setting_has_meta():
    missing = [name for name in get_editable_setting_names() if name not in META]
    assert missing == []


def test_no_stale_meta_keys():
    stale = sorted(set(META) - set(get_editable_setting_names()))
    assert stale == []


def test_sections_exist_and_are_unique():
    assert len(SECTION_IDS) == len(SECTIONS)
    assert SECTIONS[-1][0] == "advanced"
    unknown = {name: section for name, (section, _label, _help) in META.items() if section not in SECTION_IDS}
    assert unknown == {}


def test_owner_only_settings_are_advanced():
    misplaced = [name for name in get_editable_setting_names() if _owner_only(name) and META[name][0] != "advanced"]
    assert misplaced == []


def test_labels_and_help_are_filled_in():
    for name, (_section, label, help_text) in META.items():
        assert label and name not in label, name
        assert help_text.endswith((".", ")")), name


def test_unknown_setting_falls_back_to_advanced():
    assert describe("some_new_thing") == ("advanced", "Some new thing", "")
