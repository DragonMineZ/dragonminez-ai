from dataclasses import dataclass
from typing import Any


CANDIDATE_EVENT_TYPE = "dragonminez_release_candidate"
APPROVED_EVENT_TYPE = "dragonminez_release_approved"


class ReleaseCandidateError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseCandidate:
    version: str
    release_type: str
    minecraft_version: str
    forge_version: str
    commit_sha: str
    artifact_name: str
    artifact_sha256: str
    targets: tuple[str, ...]
    workflow_run_url: str | None
    changelog: str | None = None
    update_description: str | None = None


class ReleaseApprovalService:
    def __init__(self, *, github_service: Any):
        self.github_service = github_service

    async def approve_candidate(
        self,
        candidate: ReleaseCandidate,
        *,
        approved_by: str,
        changelog: str | None = None,
        update_description: str | None = None,
    ) -> None:
        dispatch_payload = build_approval_dispatch_payload(
            candidate,
            approved_by=approved_by,
            changelog=changelog,
            update_description=update_description,
        )
        await self.github_service.dispatch_repository_event(
            event_type=dispatch_payload["event_type"],
            client_payload=dispatch_payload["client_payload"],
        )


def _required_string(data: dict[str, Any], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseCandidateError(f"{field_name} is required")
    return value.strip()


def parse_release_candidate_payload(payload: dict[str, Any]) -> ReleaseCandidate:
    if payload.get("event_type") != CANDIDATE_EVENT_TYPE:
        raise ReleaseCandidateError(f"event_type must be {CANDIDATE_EVENT_TYPE}")

    client_payload = payload.get("client_payload")
    if not isinstance(client_payload, dict):
        raise ReleaseCandidateError("client_payload is required")

    targets_raw = client_payload.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ReleaseCandidateError("targets is required")
    targets = tuple(str(target).strip() for target in targets_raw if str(target).strip())
    if not targets:
        raise ReleaseCandidateError("targets is required")

    workflow_run_url_raw = client_payload.get("workflow_run_url")
    workflow_run_url = (
        workflow_run_url_raw.strip()
        if isinstance(workflow_run_url_raw, str) and workflow_run_url_raw.strip()
        else None
    )

    return ReleaseCandidate(
        version=_required_string(client_payload, "version"),
        release_type=_required_string(client_payload, "release_type"),
        minecraft_version=_required_string(client_payload, "minecraft_version"),
        forge_version=_required_string(client_payload, "forge_version"),
        commit_sha=_required_string(client_payload, "commit_sha"),
        artifact_name=_required_string(client_payload, "artifact_name"),
        artifact_sha256=_required_string(client_payload, "artifact_sha256"),
        targets=targets,
        workflow_run_url=workflow_run_url,
    )


def _optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def build_approval_dispatch_payload(
    candidate: ReleaseCandidate,
    *,
    approved_by: str,
    changelog: str | None = None,
    update_description: str | None = None,
) -> dict[str, Any]:
    client_payload: dict[str, Any] = {
        "version": candidate.version,
        "commit_sha": candidate.commit_sha,
        "artifact_sha256": candidate.artifact_sha256,
        "approved_by": approved_by,
    }

    changelog_value = _optional_string(changelog if changelog is not None else candidate.changelog)
    if changelog_value is not None:
        client_payload["changelog"] = changelog_value

    update_description_value = _optional_string(
        update_description
        if update_description is not None
        else candidate.update_description
    )
    if update_description_value is not None:
        client_payload["update_description"] = update_description_value

    return {
        "event_type": APPROVED_EVENT_TYPE,
        "client_payload": client_payload,
    }
