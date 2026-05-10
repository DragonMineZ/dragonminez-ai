import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from requests import HTTPError

from bulmaai.github.github_service import GitHubService


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        raise HTTPError(f"{self.status_code} error")


class GitHubServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_branch_ignores_already_exists_422_only(self) -> None:
        service = GitHubService(
            auth=SimpleNamespace(get_installation_token=AsyncMock(return_value="token")),
            owner="DragonMineZ",
            repo=".github",
        )

        with patch.object(service, "get_ref_sha", AsyncMock(return_value="abc123")):
            with patch(
                "bulmaai.github.github_service.request",
                AsyncMock(
                    return_value=FakeResponse(
                        422,
                        {"errors": [{"code": "already_exists"}]},
                    )
                ),
            ):
                await service.create_branch("patreon/user-1", "main")

    async def test_create_branch_raises_unexpected_422(self) -> None:
        service = GitHubService(
            auth=SimpleNamespace(get_installation_token=AsyncMock(return_value="token")),
            owner="DragonMineZ",
            repo=".github",
        )

        with patch.object(service, "get_ref_sha", AsyncMock(return_value="abc123")):
            with patch(
                "bulmaai.github.github_service.request",
                AsyncMock(
                    return_value=FakeResponse(
                        422,
                        {"errors": [{"code": "invalid"}]},
                    )
                ),
            ):
                with self.assertRaises(HTTPError):
                    await service.create_branch("patreon/bad/name", "main")


if __name__ == "__main__":
    unittest.main()
