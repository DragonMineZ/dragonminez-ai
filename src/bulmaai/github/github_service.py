import base64
from bulmaai.services.http import request
from bulmaai.github.github_app_auth import GitHubAppAuth


class GitHubService:
    def __init__(self, *, auth: GitHubAppAuth, owner: str, repo: str, base_branch: str = "main", whitelist_file_path: str | None = None):
        self.auth = auth
        self.owner = owner
        self.repo = repo
        self.base_branch = base_branch
        self.whitelist_file_path = whitelist_file_path
        self.api = f"https://api.github.com/repos/{owner}/{repo}"

    async def _headers(self) -> dict[str, str]:
        token = await self.auth.get_installation_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ==================== ISSUES ====================

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

    async def add_issue_comment(self, issue_number: int, body: str) -> dict:
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
        params: dict = {"state": state, "per_page": per_page}
        if labels:
            params["labels"] = labels
        r = await request("GET", f"{self.api}/issues", headers=await self._headers(), params=params)
        r.raise_for_status()
        return r.json()

    # ==================== BRANCHES & REFS ====================

    async def get_ref_sha(self, branch: str) -> str:
        r = await request("GET", f"{self.api}/git/ref/heads/{branch}", headers=await self._headers())
        r.raise_for_status()
        return r.json()["object"]["sha"]

    async def create_branch(self, new_branch: str, from_branch: str) -> None:
        sha = await self.get_ref_sha(from_branch)
        payload = {"ref": f"refs/heads/{new_branch}", "sha": sha}
        r = await request("POST", f"{self.api}/git/refs", headers=await self._headers(), json=payload)
        if r.status_code not in (201, 422):
            r.raise_for_status()

    async def remove_branch(self, branch: str) -> None:
        r = await request("DELETE", f"{self.api}/git/refs/heads/{branch}", headers=await self._headers())
        r.raise_for_status()

    # ==================== FILE OPERATIONS ====================

    async def get_file(self, path: str, ref: str) -> tuple[str, str]:
        r = await request("GET", f"{self.api}/contents/{path}", headers=await self._headers(), params={"ref": ref})
        r.raise_for_status()
        j = r.json()
        content = base64.b64decode(j["content"]).decode("utf-8", errors="replace")
        return content, j["sha"]

    async def put_file(self, *, path: str, branch: str, new_text: str, sha: str, message: str) -> None:
        payload = {
            "message": message,
            "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
            "sha": sha,
            "branch": branch,
        }
        r = await request("PUT", f"{self.api}/contents/{path}", headers=await self._headers(), json=payload)
        r.raise_for_status()

    # ==================== PULL REQUESTS ====================

    async def create_pr(self, *, head_branch: str, title: str, body: str) -> tuple[int, str]:
        payload = {"title": title, "head": head_branch, "base": self.base_branch, "body": body}
        r = await request("POST", f"{self.api}/pulls", headers=await self._headers(), json=payload)
        r.raise_for_status()
        j = r.json()
        return j["number"], j["html_url"]

    async def merge_pr(self, pr_number: int) -> None:
        r = await request("PUT", f"{self.api}/pulls/{pr_number}/merge", headers=await self._headers(), json={"merge_method": "squash"})
        r.raise_for_status()

    async def close_pr(self, pr_number: int) -> None:
        r = await request("PATCH", f"{self.api}/pulls/{pr_number}", headers=await self._headers(), json={"state": "closed"})
        r.raise_for_status()

    async def add_pr_comment(self, pr_number: int, comment: str) -> None:
        r = await request("POST", f"{self.api}/issues/{pr_number}/comments", headers=await self._headers(), json={"body": comment})
        r.raise_for_status()

    # ==================== WHITELIST HELPERS ====================

    async def put_whitelist_file(self, *, branch: str, new_text: str, sha: str, message: str) -> None:
        if not self.whitelist_file_path:
            raise ValueError("whitelist_file_path not configured")
        await self.put_file(path=self.whitelist_file_path, branch=branch, new_text=new_text, sha=sha, message=message)

    async def get_whitelist_file(self, ref: str) -> tuple[str, str]:
        if not self.whitelist_file_path:
            raise ValueError("whitelist_file_path not configured")
        return await self.get_file(self.whitelist_file_path, ref)

