"""Pure diagnostic helpers over a parsed :class:`DebugReport`.

These are Discord-agnostic so they can be unit tested without a bot: party
client-vs-server desync detection (section 7.6) and the warnings panel
(section 7 "Warnings panel"). The cog renders whatever these return.
"""

from __future__ import annotations

from typing import Any

from bulmaai.utils.dmzdebug_parser import DebugReport, as_bool, as_number, ticks_to_seconds

# Scope → sections that should be present, so we can warn when one is missing.
_SCOPE_SECTIONS: dict[str, tuple[str, ...]] = {
    "STATS": ("Stats", "BonusStats", "Resources", "Status", "Cooldowns", "Effects", "Skills"),
    "CHARACTER": ("Character",),
    "TECHNIQUES": ("Techniques",),
    "QUESTS": ("PlayerQuestData (raw)",),
    "ALL": (
        "Stats", "BonusStats", "Resources", "Status", "Cooldowns", "Effects",
        "Skills", "Character", "Techniques", "PlayerQuestData (raw)",
    ),
}


def _member_uuids(members: list[dict[str, Any]] | None) -> set[str]:
    if not members:
        return set()
    return {m["uuid"] for m in members if isinstance(m, dict) and m.get("uuid")}


def detect_party_desync(report: DebugReport) -> list[str]:
    """Flag client-view vs server ``PartySavedData`` mismatches (section 7.6)."""
    flags: list[str] = []
    party = report.party
    if not party:
        return flags

    client = party.get("client") or {}
    server = party.get("server") or {}

    client_in = bool(client.get("in_party"))
    server_in = bool(server.get("in_party"))

    # in-party disagreement
    if client_in and not server_in:
        flags.append("Client thinks it is in a party, but there is no server party.")
    elif server_in and not client_in:
        flags.append("Server has a party for this player, but the client is not in one.")

    # party id mismatch (only meaningful when both sides claim a party)
    if client_in and server_in:
        c_id = client.get("party_id")
        s_id = server.get("party_id")
        if c_id and s_id and c_id != s_id:
            flags.append(f"Party id mismatch: client `{c_id}` vs server `{s_id}`.")

        # leader mismatch
        c_leader = (client.get("leader") or {}).get("uuid") if client.get("leader") else None
        s_leader = (server.get("leader") or {}).get("uuid") if server.get("leader") else None
        if c_leader and s_leader and c_leader != s_leader:
            flags.append(f"Party leader mismatch: client `{c_leader}` vs server `{s_leader}`.")

        # member set difference
        c_members = _member_uuids(client.get("members"))
        s_members = _member_uuids(server.get("members"))
        if c_members != s_members:
            only_client = c_members - s_members
            only_server = s_members - c_members
            detail = []
            if only_client:
                detail.append(f"{len(only_client)} only on client")
            if only_server:
                detail.append(f"{len(only_server)} only on server")
            if detail:
                flags.append("Party member lists differ (" + ", ".join(detail) + ").")

    # pending invite
    invite = party.get("pending_invite")
    if invite:
        who = invite.get("name") or invite.get("raw") or "unknown"
        if invite.get("expired"):
            flags.append(f"Pending invite from {who} is EXPIRED.")
        else:
            flags.append(f"Pending invite present from {who}.")

    return flags


def collect_warnings(report: DebugReport, *, filename: str | None = None,
                     file_size: int | None = None) -> list[str]:
    """Build the warnings panel (section 7)."""
    warnings: list[str] = list(report.warnings)

    # Missing sections for the requested scope.
    if report.scope:
        expected = _SCOPE_SECTIONS.get(report.scope.upper())
        if expected:
            present = set(report.sections.keys())
            # Text dumps key quest data as "PlayerQuestData (raw)"; JSON as
            # "PlayerQuestData". Normalise before comparing.
            if "PlayerQuestData" in present:
                present.add("PlayerQuestData (raw)")
            missing = [s for s in expected if s not in present]
            # Quests/Party hand-written sections are tracked separately.
            if "PlayerQuestData (raw)" in missing and (report.quests is not None):
                missing.remove("PlayerQuestData (raw)")
            if missing:
                warnings.append(
                    f"Scope `{report.scope}` should include but is missing: "
                    + ", ".join(f"`{m}`" for m in missing)
                )

    status = report.sections.get("Status") or {}
    if isinstance(status, dict):
        alive = as_bool(status.get("IsAlive"))
        if alive is False:
            warnings.append("Player is dead (`IsAlive: 0`).")
        android = as_bool(status.get("AndroidUpgraded"))
        if android:
            warnings.append("`AndroidUpgraded` is set — this forces Battle Power to max.")

    # Oversized file (Discord + readability heuristic).
    if file_size is not None and file_size > 2 * 1024 * 1024:
        mb = file_size / (1024 * 1024)
        warnings.append(f"Dump is large ({mb:.1f} MB); it may be truncated in the embed.")

    return warnings


def summarize_cooldowns(report: DebugReport) -> list[tuple[str, float]]:
    """Return active cooldowns (value > 0) as ``(name, seconds)`` pairs, sorted."""
    cds = report.sections.get("Cooldowns") or {}
    if not isinstance(cds, dict):
        return []
    active: list[tuple[str, float]] = []
    for name, raw in cds.items():
        n = as_number(raw)
        if n is not None and n > 0:
            active.append((name, ticks_to_seconds(raw) or 0.0))
    active.sort(key=lambda kv: kv[1], reverse=True)
    return active


def tracked_quest_objectives(report: DebugReport) -> dict[str, Any] | None:
    """Find the tracked/accepted quest and return its objective progress.

    Returns ``{"quest_id", "status", "objectives": [(i, cur, req)], "unclaimed": [i]}``
    for the tracked quest, or ``None``. Works off ``PlayerQuestData`` (raw NBT or
    JSON), which carries the per-objective cur/req detail (section 4.10).
    """
    pqd = report.quests
    # In the text format ``report.quests`` is the hand-written summary; the raw
    # NBT lives under sections["PlayerQuestData (raw)"].
    raw = report.sections.get("PlayerQuestData (raw)")
    if raw is None and isinstance(pqd, dict) and "questState" in pqd:
        raw = pqd
    if not isinstance(raw, dict):
        return None

    tracked_id = _tracked_quest_id(report, raw)
    quest_state = raw.get("questState")
    if not isinstance(quest_state, dict):
        return None
    quests = quest_state.get("quests")
    if not isinstance(quests, list):
        return None

    target = None
    for q in quests:
        if not isinstance(q, dict):
            continue
        if tracked_id and q.get("questId") == tracked_id:
            target = q
            break
    if target is None:
        # Fall back to the first ACCEPTED quest.
        for q in quests:
            if isinstance(q, dict) and str(q.get("status", "")).upper() == "ACCEPTED":
                target = q
                break
    if target is None:
        return None

    objectives = _pair_objectives(target.get("objectives"), target.get("objectiveRequirements"))
    unclaimed = _unclaimed_rewards(target.get("rewards"))
    return {
        "quest_id": target.get("questId"),
        "status": target.get("status"),
        "objectives": objectives,
        "unclaimed": unclaimed,
    }


def _tracked_quest_id(report: DebugReport, raw: dict[str, Any]) -> str | None:
    if report.quests and isinstance(report.quests, dict):
        tracked = report.quests.get("tracked")
        if isinstance(tracked, str):
            return tracked
    quest_state = raw.get("questState")
    if isinstance(quest_state, dict):
        tid = quest_state.get("trackedQuestId")
        if isinstance(tid, str):
            return tid
    return None


def _pair_objectives(objectives: Any, requirements: Any) -> list[tuple[str, Any, Any]]:
    if not isinstance(objectives, dict):
        return []
    reqs = requirements if isinstance(requirements, dict) else {}
    out: list[tuple[str, Any, Any]] = []
    for idx in sorted(objectives.keys(), key=lambda k: str(k)):
        cur = objectives.get(idx)
        req = reqs.get(idx)
        out.append((str(idx), cur, req))
    return out


def _unclaimed_rewards(rewards: Any) -> list[str]:
    if not isinstance(rewards, dict):
        return []
    unclaimed: list[str] = []
    for idx, claimed in rewards.items():
        if as_bool(claimed) is False:
            unclaimed.append(str(idx))
    return sorted(unclaimed, key=lambda k: str(k))
