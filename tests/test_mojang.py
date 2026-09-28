import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bulmaai.services.mojang import minecraft_username_exists


class MojangLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_true_for_existing_account(self) -> None:
        with patch(
            "bulmaai.services.mojang.request",
            AsyncMock(return_value=SimpleNamespace(status_code=200)),
        ):
            self.assertIs(await minecraft_username_exists("Notch"), True)

    async def test_returns_false_for_unknown_account(self) -> None:
        with patch(
            "bulmaai.services.mojang.request",
            AsyncMock(return_value=SimpleNamespace(status_code=404)),
        ):
            self.assertIs(await minecraft_username_exists("DefinitelyNotARealAccount"), False)

    async def test_returns_none_when_lookup_is_inconclusive(self) -> None:
        with patch(
            "bulmaai.services.mojang.request",
            AsyncMock(side_effect=RuntimeError("network down")),
        ):
            self.assertIsNone(await minecraft_username_exists("SomeUser"))


if __name__ == "__main__":
    unittest.main()
