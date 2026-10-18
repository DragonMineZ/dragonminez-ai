import asyncio
import time
import jwt
import logging

from bulmaai.services.http import request

log = logging.getLogger(__name__)

# Shared across instances: most callers build a fresh GitHubAppAuth per command.
# ponytail: one global lock, fine for a single installation; per-key locks if that changes.
_tokens: dict[tuple[str, str], tuple[str, int]] = {}
_token_lock = asyncio.Lock()


class GitHubAppAuth:
    def __init__(self, *, app_id: str, installation_id: str, private_key_pem: str):
        if not app_id or not app_id.strip():
            raise ValueError("app_id cannot be empty")
        if not installation_id or not installation_id.strip():
            raise ValueError("installation_id cannot be empty")
        if not private_key_pem or not private_key_pem.strip():
            raise ValueError("private_key_pem cannot be empty")

        self.app_id = app_id.strip()
        self.installation_id = installation_id.strip()
        self.private_key_pem = private_key_pem

    def _make_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 9 * 60,  # keep under 10 minutes
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    async def get_installation_token(self) -> str:
        key = (self.app_id, self.installation_id)
        async with _token_lock:
            cached = _tokens.get(key)
            if cached and time.time() < cached[1] - 60:
                return cached[0]
            token = await self._fetch_token()
            _tokens[key] = (token, int(time.time()) + 45 * 60)
            return token

    async def _fetch_token(self) -> str:
        gh_jwt = self._make_jwt()
        url = f"https://api.github.com/app/installations/{self.installation_id}/access_tokens"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {gh_jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        log.info(f"Requesting GitHub installation token from: {url}")
        try:
            r = await request("POST", url, headers=headers, json={})
            r.raise_for_status()
            token = r.json()["token"]
            log.info("Successfully obtained GitHub installation token")
            return token
        except Exception as e:
            # Provide specific error messages for common issues
            error_msg = f"Failed to get GitHub installation token"

            if hasattr(e, 'response') and e.response is not None:
                status = e.response.status_code
                if status == 404:
                    error_msg += f" | 404 Not Found - Check that:"
                    error_msg += f"\n  - Installation ID '{self.installation_id}' is correct"
                    error_msg += f"\n  - The GitHub App is installed on your organization/repo"
                    error_msg += f"\n  - App ID '{self.app_id}' is correct"
                elif status == 401:
                    error_msg += f" | 401 Unauthorized - Check that:"
                    error_msg += f"\n  - Your private key is correct and matches the App ID"
                    error_msg += f"\n  - The JWT is generated correctly"
                else:
                    error_msg += f" | Status: {status}"

                try:
                    error_body = e.response.text
                    error_msg += f"\n  - Response: {error_body}"
                except:
                    pass
            else:
                error_msg += f": {e}"

            log.error(error_msg)
            raise RuntimeError(error_msg) from e
