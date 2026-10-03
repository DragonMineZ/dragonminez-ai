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
        if self.status_code < 400:
            return
        error = HTTPError(f"{self.status_code} error")
        error.response = self
        raise error


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

    async def test_search_text_cannot_add_qualifiers_that_widen_the_scope(self) -> None:
        service = GitHubService(
            auth=SimpleNamespace(get_installation_token=AsyncMock(return_value="token")),
            owner="DragonMineZ",
            repo="dragonminez",
        )
        with patch(
            "bulmaai.github.github_service.request",
            AsyncMock(return_value=FakeResponse(200, {"items": []})),
        ) as request_mock:
            await service.search_issues("crash error: NPE OR repo:DragonMineZ/private-repo user:evil")

        self.assertEqual(
            request_mock.await_args.kwargs["params"]["q"],
            "repo:DragonMineZ/dragonminez is:issue crash error: NPE OR",
        )

    async def test_reset_branch_sends_force_update(self) -> None:
        service = GitHubService(
            auth=SimpleNamespace(get_installation_token=AsyncMock(return_value="token")),
            owner="DragonMineZ",
            repo=".github",
        )

        with patch(
            "bulmaai.github.github_service.request",
            AsyncMock(return_value=FakeResponse(200, {})),
        ) as request_mock:
            await service.reset_branch("patreon/user-1", "abc123")

        request_mock.assert_awaited_once()
        args, kwargs = request_mock.await_args
        self.assertEqual(args[0], "PATCH")
        self.assertTrue(args[1].endswith("/git/refs/heads/patreon/user-1"))
        self.assertEqual(kwargs["json"], {"sha": "abc123", "force": True})

    async def test_create_or_get_pr_returns_existing_open_pr_on_duplicate_422(self) -> None:
        service = GitHubService(
            auth=SimpleNamespace(get_installation_token=AsyncMock(return_value="token")),
            owner="DragonMineZ",
            repo=".github",
        )
        duplicate_response = FakeResponse(
            422,
            {
                "message": "Validation Failed",
                "errors": [
                    {
                        "resource": "PullRequest",
                        "code": "custom",
                        "message": "A pull request already exists for DragonMineZ:patreon/user-456.",
                    }
                ],
            },
        )
        existing_pr = {"number": 12, "html_url": "https://example.test/pr/12"}

        with patch(
            "bulmaai.github.github_service.request",
            AsyncMock(side_effect=[duplicate_response, FakeResponse(200, [existing_pr])]),
        ):
            pr = await service.create_or_get_pr(
                head_branch="patreon/user-456",
                title="Add beta tester: NewTester",
                body="Requested by Discord user Requester (456).",
            )

        self.assertEqual(pr, existing_pr)


if __name__ == "__main__":
    unittest.main()
