import time
import jwt

from src.bulmaai.services.http import request


class GitHubAppAuth:
    def __init__(self, *, app_id: str, installation_id: str, private_key_pem: str):
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_pem = private_key_pem

        self._token: str | None = None
        self._expires_epoch: int = 0

    def _make_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 9 * 60,  # keep under 10 minutes
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    async def get_installation_token(self) -> str:
        now = int(time.time())
        if self._token and now < (self._expires_epoch - 60):
            return self._token

        gh_jwt = self._make_jwt()
        url = f"https://api.github.com/app/installations/{self.installation_id}/access_tokens"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {gh_jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        r = await request("POST", url, headers=headers)
        r.raise_for_status()
        data = r.json()

        self._token = data["token"]
        self._expires_epoch = int(time.time()) + 45 * 60
        return self._token
