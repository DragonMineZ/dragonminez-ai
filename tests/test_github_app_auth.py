import unittest
from unittest.mock import AsyncMock, patch

from bulmaai.github import github_app_auth
from bulmaai.github.github_app_auth import GitHubAppAuth


class InstallationTokenCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        github_app_auth._tokens.clear()

    async def test_token_is_shared_across_instances(self):
        fetch = AsyncMock(return_value="tok")
        with patch.object(GitHubAppAuth, "_fetch_token", fetch):
            for _ in range(3):
                auth = GitHubAppAuth(app_id="1", installation_id="2", private_key_pem="pem")
                self.assertEqual(await auth.get_installation_token(), "tok")
        fetch.assert_awaited_once()

    async def test_expired_token_is_refetched(self):
        github_app_auth._tokens[("1", "2")] = ("old", 0)
        fetch = AsyncMock(return_value="new")
        with patch.object(GitHubAppAuth, "_fetch_token", fetch):
            auth = GitHubAppAuth(app_id="1", installation_id="2", private_key_pem="pem")
            self.assertEqual(await auth.get_installation_token(), "new")
