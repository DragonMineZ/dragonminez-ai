from bulmaai.services.http import request
from bulmaai.github.github_app_auth import GitHubAppAuth


class GitHubIssuesService:
    def __init__(self, *, auth: GitHubAppAuth, owner: str, repo: str):
        self.auth = auth
        self.owner = owner
        self.repo = repo
        self.api = f"https://api.github.com/repos/{owner}/{repo}"

    async def _headers(self) -> dict[str, str]:
        token = await self.auth.get_installation_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_labels(self) -> list[dict]:
        r = await request("GET", f"{self.api}/labels", headers=await self._headers(), params={"per_page": 100})
        r.raise_for_status()
        return r.json()

    async def create_issue(self, *, title: str, body: str, labels: list[str] | None = None) -> dict:
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        r = await request("POST", f"{self.api}/issues", headers=await self._headers(), json=payload)
        r.raise_for_status()
        return r.json()

    async def get_issue(self, issue_number: int) -> dict:
        r = await request("GET", f"{self.api}/issues/{issue_number}", headers=await self._headers())
        r.raise_for_status()
        return r.json()

    async def close_issue(self, issue_number: int, *, reason: str = "completed") -> dict:
        payload = {"state": "closed", "state_reason": reason}
        r = await request("PATCH", f"{self.api}/issues/{issue_number}", headers=await self._headers(), json=payload)
        r.raise_for_status()
        return r.json()

    async def reopen_issue(self, issue_number: int) -> dict:
        payload = {"state": "open"}
        r = await request("PATCH", f"{self.api}/issues/{issue_number}", headers=await self._headers(), json=payload)
        r.raise_for_status()
        return r.json()

    async def add_comment(self, issue_number: int, body: str) -> dict:
        r = await request("POST", f"{self.api}/issues/{issue_number}/comments", headers=await self._headers(), json={"body": body})
        r.raise_for_status()
        return r.json()

    async def add_labels(self, issue_number: int, labels: list[str]) -> list[dict]:
        r = await request("POST", f"{self.api}/issues/{issue_number}/labels", headers=await self._headers(), json={"labels": labels})
        r.raise_for_status()
        return r.json()

    async def remove_label(self, issue_number: int, label: str) -> None:
        r = await request("DELETE", f"{self.api}/issues/{issue_number}/labels/{label}", headers=await self._headers())
        if r.status_code != 404:
            r.raise_for_status()

    async def assign_issue(self, issue_number: int, assignees: list[str]) -> dict:
        r = await request("POST", f"{self.api}/issues/{issue_number}/assignees", headers=await self._headers(), json={"assignees": assignees})
        r.raise_for_status()
        return r.json()

    async def list_issues(self, *, state: str = "open", labels: str | None = None, per_page: int = 25) -> list[dict]:
        params = {"state": state, "per_page": per_page}
        if labels:
            params["labels"] = labels
        r = await request("GET", f"{self.api}/issues", headers=await self._headers(), params=params)
        r.raise_for_status()
        return r.json()


