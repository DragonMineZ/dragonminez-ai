"""Components V2 cards + buttons for the build gate. Buttons carry the request id in their custom_id and hold no state, so
they keep working after a restart (clicks are routed by the cog's on_interaction listener)."""

from datetime import datetime

import discord

from bulmaai.services import build_gate
from bulmaai.ui.dev_jar_views import whats_new_container

APPROVE_PREFIX = "build_gate:approve:"
REJECT_PREFIX = "build_gate:reject:"
CHANGELOG_PREFIX = "build_gate:changelog:"

BLURPLE = 0x5865F2
GREEN = 0x57F287
RED = 0xED4245
GREY = 0x99AAB5
AMBER = 0xFEE75C
MAX_COMMIT_LINES = 8


def changelog_button(request: build_gate.BuildRequest) -> discord.ui.Button:
    return discord.ui.Button(
        label="Edit changelog" if request.changelog else "Add changelog",
        style=discord.ButtonStyle.primary,
        custom_id=f"{CHANGELOG_PREFIX}{request.id}",
    )


def gate_buttons(request: build_gate.BuildRequest) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(
        discord.ui.Button(label="Build jar", style=discord.ButtonStyle.success, custom_id=f"{APPROVE_PREFIX}{request.id}"),
        changelog_button(request),
        discord.ui.Button(label="Skip", style=discord.ButtonStyle.secondary, custom_id=f"{REJECT_PREFIX}{request.id}"),
    )


def _card(text: str, color: int, *rows: discord.ui.ActionRow) -> discord.ui.DesignerView:
    return discord.ui.DesignerView(discord.ui.Container(discord.ui.TextDisplay(text[:3900]), *rows, color=color), timeout=None)


class ChangelogModal(discord.ui.Modal):
    def __init__(self, request: build_gate.BuildRequest, on_submit):
        super().__init__(title="Dev jar changelog")
        self._request_id = request.id
        self._on_submit = on_submit
        self.changelog_input = discord.ui.InputText(
            label="Changelog (shown as What's New)",
            placeholder="Leave empty for no changelog. Discord markdown works.",
            style=discord.InputTextStyle.long,
            required=False,
            max_length=build_gate.MAX_CHANGELOG_CHARS,
            value=request.changelog or "",
        )
        self.add_item(self.changelog_input)

    async def callback(self, interaction: discord.Interaction):
        await self._on_submit(interaction, self._request_id, self.changelog_input.value)


def preview_card(request: build_gate.BuildRequest, *, locked: bool = False) -> discord.ui.DesignerView:
    """What's New exactly as the public dev jar post will show it, plus a note (and the edit button until locked)."""
    if request.changelog:
        view = discord.ui.DesignerView(whats_new_container(request.changelog), timeout=None)
        note = "Final: this is what the public post will show." if locked else "This is how What's New will look in the public post. Press Edit changelog to change it until the build finishes."
    else:
        view = _card("### No changelog\nThe public post will have no What's New section.", GREY)
        note = "Final: no changelog." if locked else "Press Edit changelog to add one until the build finishes."
    container = view.children[0]
    container.add_item(discord.ui.TextDisplay(f"-# Build #{request.id} · {note}"))
    if not locked:
        container.add_item(discord.ui.ActionRow(changelog_button(request)))
    return view


def _ts(moment: datetime, style: str = "R") -> str:
    return f"<t:{int(moment.timestamp())}:{style}>"


def commit_lines(commits, *, limit: int = MAX_COMMIT_LINES) -> str:
    """Newest first: the latest push is always the top line, with its title and author."""
    ordered = list(reversed(commits))
    lines = [f"`{c['sha'][:7]}` {c['title'][:70]} — {c['author']}" for c in ordered[:limit]]
    if len(ordered) > limit:
        lines.append(f"…and {len(ordered) - limit} more")
    return "\n".join(lines) or "No commit info."


def _header(request: build_gate.BuildRequest) -> str:
    return f"**{request.branch}** · `{request.head_sha[:7]}` · pushed by {request.pusher}"


def gate_card(request: build_gate.BuildRequest, *, recent: list[build_gate.BuildRequest], window_minutes: int) -> discord.ui.DesignerView:
    parts = [
        "### 🔨 Build a dev jar for this push?",
        f"{_header(request)}\nAuto-closes {_ts(request.expires_at)} if nobody answers.",
        f"**Commits (latest first)**\n{commit_lines(request.commits)}",
    ]
    if request.changelog:
        text = request.changelog
        parts.append(f"**Changelog**\n{text if len(text) <= 1000 else text[:999].rstrip() + '…'}")
    if recent:
        lines = [
            f"#{r.id} · **{r.branch}** · {r.commits[-1]['title'][:50] if r.commits else r.head_sha[:7]} — {r.pusher} · *{r.status}*"
            for r in recent[:5]
        ]
        parts.append(
            f"**⚠️ {len(recent)} other push(es) in the last {window_minutes} min**\n"
            + "\n".join(lines)
            + "\nOne run builds this push's commits; pending pushes on the same branch are folded in."
        )
    return _card("\n".join(parts), BLURPLE, gate_buttons(request))


def progress_card(
    request: build_gate.BuildRequest, *, job: dict | None, run: dict | None, approver_id: int | None, note: str | None = None
) -> discord.ui.DesignerView:
    steps = build_gate.visible_steps(job)
    done = sum(1 for s in steps if s["status"] == "completed")
    parts = ["### ⚙️ Building dev jar…", _header(request)]
    if approver_id:
        parts.append(f"Approved by <@{approver_id}>")
    if steps:
        parts.append(f"**Progress** {build_gate.progress_bar(done, len(steps))}")
        parts.append("\n".join(f"{build_gate.step_icon(s)} {s['name']}" for s in steps)[:1500])
    else:
        parts.append("**Progress** ⏳ Queued, waiting for a runner…")
    links = []
    if run and run.get("created_at"):
        started = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        links.append(f"**Started** {_ts(started)}")
    if request.run_url:
        links.append(f"[View on GitHub](<{request.run_url}>)")
    if links:
        parts.append(" · ".join(links))
    if note:
        parts.append(f"-# {note}")
    return _card("\n".join(parts), AMBER)


def final_card(request: build_gate.BuildRequest, *, status: str, note: str | None = None, job: dict | None = None) -> discord.ui.DesignerView:
    title, color = {
        build_gate.SUCCEEDED: ("✅ Dev jar built", GREEN),
        build_gate.FAILED: ("❌ Dev jar build failed", RED),
        build_gate.REJECTED: ("⏭️ Build skipped", GREY),
        build_gate.EXPIRED: ("⌛ Build prompt closed (no answer)", GREY),
        build_gate.SUPERSEDED: ("🔁 Replaced by a newer push", GREY),
    }[status]
    parts = [f"### {title}", _header(request)]
    if status == build_gate.SUCCEEDED:
        steps = build_gate.visible_steps(job)
        if steps:
            parts.append(f"**Progress** {build_gate.progress_bar(len(steps), len(steps))}")
        parts.append("**Next** The usual staff review message with Publish / Discard follows.")
    elif status == build_gate.FAILED:
        failed = [s["name"] for s in build_gate.visible_steps(job) if s["status"] == "completed" and s.get("conclusion") not in ("success", "skipped")]
        if failed:
            parts.append(f"**Failed step** {failed[0]}")
    if request.run_url:
        parts.append(f"[View on GitHub](<{request.run_url}>)")
    if request.commits and status in (build_gate.SUCCEEDED, build_gate.FAILED):
        parts.append(f"**Commits (latest first)**\n{commit_lines(request.commits, limit=4)}")
    if note:
        parts.append(f"-# {note}")
    return _card("\n".join(parts), color)
