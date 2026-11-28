import base64
import re
from urllib.parse import quote

from bulmaai.services.http import request
from bulmaai.github.github_app_auth import GitHubAppAuth

_SEARCH_QUALIFIER_RE = re.compile(r"(?i)(?<!\S)-?[a-z][a-z_-]*:\S+")


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

    async def dispatch_repository_event(self, *, event_type: str, client_payload: dict) -> None:
        payload = {
            "event_type": event_type,
            "client_payload": client_payload,
        }
        r = await request("POST", f"{self.api}/dispatches", headers=await self._headers(), json=payload)
        r.raise_for_status()

    # ==================== ACTIONS ====================

    async def dispatch_workflow(self, *, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        r = await request(
            "POST",
            f"{self.api}/actions/workflows/{workflow}/dispatches",
            headers=await self._headers(),
            json={"ref": ref, "inputs": inputs},
        )
        r.raise_for_status()

    async def list_workflow_runs(self, workflow: str, *, event: str = "workflow_dispatch", per_page: int = 10) -> list[dict]:
        r = await request(
            "GET",
            f"{self.api}/actions/workflows/{workflow}/runs",
            headers=await self._headers(),
            params={"event": event, "per_page": per_page},
        )
        r.raise_for_status()
        return r.json().get("workflow_runs", [])

    async def get_workflow_run(self, run_id: int) -> dict:
        r = await request("GET", f"{self.api}/actions/runs/{run_id}", headers=await self._headers())
        r.raise_for_status()
        return r.json()

    async def list_run_jobs(self, run_id: int) -> list[dict]:
        r = await request("GET", f"{self.api}/actions/runs/{run_id}/jobs", headers=await self._headers())
        r.raise_for_status()
        return r.json().get("jobs", [])

    async def list_repo_events(self, *, per_page: int = 50) -> list[dict]:
        r = await request(
            "GET", f"{self.api}/events", headers=await self._headers(), params={"per_page": per_page}
        )
        r.raise_for_status()
        return r.json()

    async def get_commit(self, sha: str) -> dict:
        r = await request("GET", f"{self.api}/commits/{sha}", headers=await self._headers())
        r.raise_for_status()
        return r.json()

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

    async def search_issues(self, text: str, *, per_page: int = 10) -> list[dict]:
        """Full-text search issues (open and closed) in this repo, most relevant first."""
        # The text comes from members/the model: drop qualifiers like repo:/org:/user: so it can't widen the scope.
        text = " ".join(_SEARCH_QUALIFIER_RE.sub(" ", text).split())
        query = f"repo:{self.owner}/{self.repo} is:issue {text}".strip()
        r = await request(
            "GET",
            "https://api.github.com/search/issues",
            headers=await self._headers(),
            params={"q": query, "per_page": per_page, "sort": "updated", "order": "desc"},
        )
        r.raise_for_status()
        return r.json().get("items", [])

    # ==================== FILE OPERATIONS ====================

    async def get_file(self, path: str, ref: str, *, strict: bool = False) -> tuple[str, str]:
        """strict: raise UnicodeDecodeError instead of replacing bad bytes (for files that get written back)."""
        r = await request("GET", f"{self.api}/contents/{path}", headers=await self._headers(), params={"ref": ref})
        r.raise_for_status()
        j = r.json()
        content = base64.b64decode(j["content"]).decode("utf-8", errors="strict" if strict else "replace")
        return content, j["sha"]

    async def list_dir(self, path: str, ref: str) -> list[str]:
        r = await request("GET", f"{self.api}/contents/{path}", headers=await self._headers(), params={"ref": ref})
        r.raise_for_status()
        j = r.json()
        return [item["path"] for item in j if item.get("type") == "file"] if isinstance(j, list) else []

    async def list_tree(self, ref: str) -> list[str]:
        """Every file path on ref, in one call (the recursive Git tree)."""
        r = await request(
            "GET", f"{self.api}/git/trees/{quote(ref, safe='')}", headers=await self._headers(), params={"recursive": "1"}
        )
        r.raise_for_status()
        data = r.json()
        if data.get("truncated"):
            raise RuntimeError(f"The file tree of {ref} is too big to list in one call")
        return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]

    async def put_file(self, *, path: str, branch: str, new_text: str, sha: str, message: str) -> str:
        """Commit straight to `branch`. A stale `sha` gets a 409 instead of clobbering someone else's commit. Returns the commit URL."""
        payload = {
            "message": message,
            "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
            "sha": sha,
            "branch": branch,
        }
        r = await request("PUT", f"{self.api}/contents/{path}", headers=await self._headers(), json=payload)
        r.raise_for_status()
        return r.json()["commit"]["html_url"]

    async def head_sha(self, branch: str) -> str:
        r = await request("GET", f"{self.api}/git/ref/heads/{quote(branch, safe='')}", headers=await self._headers())
        r.raise_for_status()
        return r.json()["object"]["sha"]

    async def open_pr_with_files(
        self, *, base_sha: str, base: str, branch: str, files: dict[str, bytes], message: str, title: str, body: str
    ) -> str:
        """One commit on top of base_sha with files (path -> bytes, text or binary) on branch, then a PR into base.
        Returns the PR's URL. Safe to retry: an open PR from branch is returned as is, and a branch left over by a
        failed attempt is moved to the new commit. Never merges."""
        headers = await self._headers()
        existing = await self.list_prs(state="open", per_page=1, head=f"{self.owner}:{branch}")
        if existing:
            return existing[0]["html_url"]
        r = await request("GET", f"{self.api}/git/commits/{base_sha}", headers=headers)
        r.raise_for_status()
        tree = []
        for path, content in files.items():
            blob = await request(
                "POST", f"{self.api}/git/blobs", headers=headers,
                json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            )
            blob.raise_for_status()
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})
        made = await request(
            "POST", f"{self.api}/git/trees", headers=headers, json={"base_tree": r.json()["tree"]["sha"], "tree": tree}
        )
        made.raise_for_status()
        commit = await request(
            "POST", f"{self.api}/git/commits", headers=headers,
            json={"message": message, "tree": made.json()["sha"], "parents": [base_sha]},
        )
        commit.raise_for_status()
        commit_sha = commit.json()["sha"]
        ref = await request(
            "POST", f"{self.api}/git/refs", headers=headers, json={"ref": f"refs/heads/{branch}", "sha": commit_sha}
        )
        if ref.status_code == 422:  # left over from a failed attempt: point it at this commit
            ref = await request(
                "PATCH", f"{self.api}/git/refs/heads/{quote(branch, safe='/')}", headers=headers,
                json={"sha": commit_sha, "force": True},
            )
        ref.raise_for_status()
        pr = await request(
            "POST", f"{self.api}/pulls", headers=headers, json={"title": title, "head": branch, "base": base, "body": body}
        )
        pr.raise_for_status()
        return pr.json()["html_url"]

    # ==================== PULL REQUESTS ====================

    async def list_prs(self, *, state: str = "open", per_page: int = 25, head: str | None = None) -> list[dict]:
        params = {"state": state, "per_page": per_page}
        if head:
            params["head"] = head
        r = await request("GET", f"{self.api}/pulls", headers=await self._headers(), params=params)
        r.raise_for_status()
        return r.json()

    async def get_pr(self, pr_number: int) -> dict:
        r = await request("GET", f"{self.api}/pulls/{pr_number}", headers=await self._headers())
        r.raise_for_status()
        return r.json()

    async def merge_pr(self, pr_number: int, *, merge_method: str = "squash") -> dict:
        r = await request("PUT", f"{self.api}/pulls/{pr_number}/merge", headers=await self._headers(), json={"merge_method": merge_method})
        r.raise_for_status()
        return r.json()

    async def close_pr(self, pr_number: int) -> dict:
        r = await request("PATCH", f"{self.api}/pulls/{pr_number}", headers=await self._headers(), json={"state": "closed"})
        r.raise_for_status()
        return r.json()

    async def reopen_pr(self, pr_number: int) -> dict:
        r = await request("PATCH", f"{self.api}/pulls/{pr_number}", headers=await self._headers(), json={"state": "open"})
        r.raise_for_status()
        return r.json()

    async def add_pr_comment(self, pr_number: int, comment: str) -> dict:
        r = await request("POST", f"{self.api}/issues/{pr_number}/comments", headers=await self._headers(), json={"body": comment})
        r.raise_for_status()
        return r.json()

    # ==================== WHITELIST HELPERS ====================

    async def put_whitelist_file(self, *, branch: str, new_text: str, sha: str, message: str) -> str:
        if not self.whitelist_file_path:
            raise ValueError("whitelist_file_path not configured")
        return await self.put_file(path=self.whitelist_file_path, branch=branch, new_text=new_text, sha=sha, message=message)

    async def get_whitelist_file(self, ref: str) -> tuple[str, str]:
        if not self.whitelist_file_path:
            raise ValueError("whitelist_file_path not configured")
        return await self.get_file(self.whitelist_file_path, ref)

