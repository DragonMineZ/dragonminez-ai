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


def _service(repo: str = ".github") -> GitHubService:
    return GitHubService(
        auth=SimpleNamespace(get_installation_token=AsyncMock(return_value="token")),
        owner="DragonMineZ",
        repo=repo,
    )


class GitHubServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_text_cannot_add_qualifiers_that_widen_the_scope(self) -> None:
        service = _service("dragonminez")
        with patch(
            "bulmaai.github.github_service.request",
            AsyncMock(return_value=FakeResponse(200, {"items": []})),
        ) as request_mock:
            await service.search_issues("crash error: NPE OR repo:DragonMineZ/private-repo user:evil")

        self.assertEqual(
            request_mock.await_args.kwargs["params"]["q"],
            "repo:DragonMineZ/dragonminez is:issue crash error: NPE OR",
        )

    async def test_put_file_commits_to_branch_and_returns_commit_url(self) -> None:
        service = _service()
        with patch(
            "bulmaai.github.github_service.request",
            AsyncMock(return_value=FakeResponse(200, {"commit": {"html_url": "https://example.test/commit/abc"}})),
        ) as request_mock:
            url = await service.put_file(path="allowed.txt", branch="main", new_text="A\n", sha="s1", message="m")

        self.assertEqual(url, "https://example.test/commit/abc")
        args, kwargs = request_mock.await_args
        self.assertEqual(args[0], "PUT")
        self.assertEqual(kwargs["json"]["branch"], "main")
        self.assertEqual(kwargs["json"]["sha"], "s1")

    async def test_put_file_stale_sha_raises_409(self) -> None:
        service = _service()
        with patch(
            "bulmaai.github.github_service.request",
            AsyncMock(return_value=FakeResponse(409, {"message": "is at x but expected y"})),
        ):
            with self.assertRaises(HTTPError) as caught:
                await service.put_file(path="allowed.txt", branch="main", new_text="A\n", sha="old", message="m")
        self.assertEqual(caught.exception.response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
