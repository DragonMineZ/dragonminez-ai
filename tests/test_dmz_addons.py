import unittest
from unittest.mock import patch

from bulmaai.utils.dmz_addons import check_addons, _version_tuple

FAKE_ADDONS = {"some_addon": "1.2.0"}


class VersionTupleTests(unittest.TestCase):
    def test_extracts_numeric_parts(self) -> None:
        self.assertEqual(_version_tuple("1.20.1-3.2.0"), (1, 20, 1, 3, 2, 0))

    def test_no_digits_yields_empty(self) -> None:
        self.assertEqual(_version_tuple("unknown"), ())


class CheckAddonsTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(
            "bulmaai.utils.dmz_addons.KNOWN_ADDONS", FAKE_ADDONS, clear=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mods = {"some_addon": "9.9.9", "unrelated_mod": "1.0.0"}

    def test_ignores_unknown_mods(self) -> None:
        results = check_addons(self.mods, dmz_version="2.0.0")

        self.assertEqual([r.mod_id for r in results], ["some_addon"])

    def test_newer_dmz_is_compatible(self) -> None:
        results = check_addons(self.mods, dmz_version="2.0.0")

        self.assertTrue(results[0].compatible)

    def test_older_dmz_is_incompatible(self) -> None:
        results = check_addons(self.mods, dmz_version="0.0.1")

        self.assertFalse(results[0].compatible)

    def test_unknown_dmz_version_is_undecided(self) -> None:
        results = check_addons(self.mods, dmz_version=None)

        self.assertIsNone(results[0].compatible)
        self.assertEqual(results[0].min_dmz_version, "1.2.0")


if __name__ == "__main__":
    unittest.main()
