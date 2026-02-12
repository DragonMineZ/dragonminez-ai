import base64

from bulmaai.services.http import request
from bulmaai.github.github_app_auth import GitHubAppAuth


class GitHubWhitelistService:
    def __init__(self, *, auth: GitHubAppAuth, owner: str, repo: str, base_branch: str, file_path: str):
        self.auth = auth
        self.owner = owner
        self.repo = repo
        self.base_branch = base_branch
        self.file_path = file_path
        self.api = f"https://api.github.com/repos/{owner}/{repo}"

    async def _headers(self) -> dict[str, str]:
        token = await self.auth.get_installation_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_ref_sha(self, branch: str) -> str:
        r = await request("GET", f"{self.api}/git/ref/heads/{branch}", headers=await self._headers())
        r.raise_for_status()
        return r.json()["object"]["sha"]

    async def create_branch(self, new_branch: str, from_branch: str) -> None:
        sha = await self.get_ref_sha(from_branch)
        payload = {"ref": f"refs/heads/{new_branch}", "sha": sha}
        r = await request("POST", f"{self.api}/git/refs", headers=await self._headers(), json=payload)
        if r.status_code not in (201, 422):  # 422 if branch exists
            r.raise_for_status()

    async def get_file(self, path: str, ref: str):
        r = await request("GET", f"{self.api}/contents/{path}", headers=await self._headers(), params={"ref": ref})
        r.raise_for_status()
        j = r.json()
        content = base64.b64decode(j["content"]).decode("utf-8", errors="replace")
        return content, j["sha"]

    async def put_file(self, *, branch: str, new_text: str, sha: str, message: str) -> None:
        payload = {
            "message": message,
            "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
            "sha": sha,
            "branch": branch,
        }
        r = await request("PUT", f"{self.api}/contents/{self.file_path}", headers=await self._headers(), json=payload)
        r.raise_for_status()

    async def create_pr(self, *, head_branch: str, title: str, body: str):
        payload = {"title": title, "head": head_branch, "base": self.base_branch, "body": body}
        r = await request("POST", f"{self.api}/pulls", headers=await self._headers(), json=payload)
        r.raise_for_status()
        j = r.json()
        return j["number"], j["html_url"]

    async def merge_pr(self, pr_number: int) -> None:
        r = await request("PUT", f"{self.api}/pulls/{pr_number}/merge",
                          headers=await self._headers(), json={"merge_method": "squash"})
        r.raise_for_status()

    async def close_pr(self, pr_number: int) -> None:
        r = await request("PATCH", f"{self.api}/pulls/{pr_number}",
                          headers=await self._headers(), json={"state": "closed"})
        r.raise_for_status()

    async def add_comment(self, pr_number: int, comment: str) -> None:
        r = await request("POST", f"{self.api}/issues/{pr_number}/comments",
                          headers=await self._headers(), json={"body": comment})
        r.raise_for_status()

    async def remove_branch(self, branch: str) -> None:
        r = await request("DELETE",f"{self.api}/git/refs/heads/{branch}",
                          headers=await self._headers(),
        )
        r.raise_for_status()
