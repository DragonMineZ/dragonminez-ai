"""Embeds + buttons for the build gate. Buttons carry the request id in their custom_id and hold no state, so
they keep working after a restart (clicks are routed by the cog's on_interaction listener)."""

from datetime import datetime

import discord

from bulmaai.services import build_gate
from bulmaai.ui.dev_jar_views import build_whats_new_embed

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


def gate_view(request: build_gate.BuildRequest) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Build jar", style=discord.ButtonStyle.success, custom_id=f"{APPROVE_PREFIX}{request.id}"))
    view.add_item(changelog_button(request))
    view.add_item(discord.ui.Button(label="Skip", style=discord.ButtonStyle.secondary, custom_id=f"{REJECT_PREFIX}{request.id}"))
    return view


def preview_view(request: build_gate.BuildRequest) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(changelog_button(request))
    return view


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


def preview_embeds(request: build_gate.BuildRequest, *, locked: bool = False) -> list[discord.Embed]:
    if request.changelog:
        embeds = [build_whats_new_embed(request.changelog)]
        note = "Final: this is what the public post will show." if locked else "This is how What's New will look in the public post. Press Edit changelog to change it until the build finishes."
    else:
        embeds = [discord.Embed(title="No changelog", description="The public post will have no What's New section.", color=GREY)]
        note = "Final: no changelog." if locked else "Press Edit changelog to add one until the build finishes."
    embeds[-1].set_footer(text=f"Build #{request.id} · {note}")
    return embeds


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


def gate_embed(request: build_gate.BuildRequest, *, recent: list[build_gate.BuildRequest], window_minutes: int) -> discord.Embed:
    embed = discord.Embed(
        title="🔨 Build a dev jar for this push?",
        description=f"{_header(request)}\nAuto-closes {_ts(request.expires_at)} if nobody answers.",
        color=BLURPLE,
    )
    embed.add_field(name="Commits (latest first)", value=commit_lines(request.commits), inline=False)
    if request.changelog:
        text = request.changelog
        embed.add_field(name="Changelog", value=text if len(text) <= 1024 else text[:1023].rstrip() + "…", inline=False)
    if recent:
        lines = [
            f"#{r.id} · **{r.branch}** · {r.commits[-1]['title'][:50] if r.commits else r.head_sha[:7]} — {r.pusher} · *{r.status}*"
            for r in recent[:5]
        ]
        embed.add_field(
            name=f"⚠️ {len(recent)} other push(es) in the last {window_minutes} min",
            value="\n".join(lines) + "\nOne run builds this push's commits; pending pushes on the same branch are folded in.",
            inline=False,
        )
    return embed


def progress_embed(
    request: build_gate.BuildRequest, *, job: dict | None, run: dict | None, approver_id: int | None, note: str | None = None
) -> discord.Embed:
    steps = build_gate.visible_steps(job)
    done = sum(1 for s in steps if s["status"] == "completed")
    embed = discord.Embed(title="⚙️ Building dev jar…", description=_header(request), color=AMBER)
    if approver_id:
        embed.description += f"\nApproved by <@{approver_id}>"
    if steps:
        embed.add_field(name="Progress", value=build_gate.progress_bar(done, len(steps)), inline=False)
        embed.add_field(name="Steps", value="\n".join(f"{build_gate.step_icon(s)} {s['name']}" for s in steps)[:1024], inline=False)
    else:
        embed.add_field(name="Progress", value="⏳ Queued, waiting for a runner…", inline=False)
    if run and run.get("created_at"):
        started = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        embed.add_field(name="Started", value=_ts(started), inline=True)
    if request.run_url:
        embed.add_field(name="Run", value=f"[View on GitHub]({request.run_url})", inline=True)
    if note:
        embed.set_footer(text=note)
    return embed


def final_embed(request: build_gate.BuildRequest, *, status: str, note: str | None = None, job: dict | None = None) -> discord.Embed:
    title, color = {
        build_gate.SUCCEEDED: ("✅ Dev jar built", GREEN),
        build_gate.FAILED: ("❌ Dev jar build failed", RED),
        build_gate.REJECTED: ("⏭️ Build skipped", GREY),
        build_gate.EXPIRED: ("⌛ Build prompt closed (no answer)", GREY),
        build_gate.SUPERSEDED: ("🔁 Replaced by a newer push", GREY),
    }[status]
    embed = discord.Embed(title=title, description=_header(request), color=color)
    if status == build_gate.SUCCEEDED:
        steps = build_gate.visible_steps(job)
        if steps:
            embed.add_field(name="Progress", value=build_gate.progress_bar(len(steps), len(steps)), inline=False)
        embed.add_field(name="Next", value="The usual staff review message with Publish / Discard follows.", inline=False)
    elif status == build_gate.FAILED:
        failed = [s["name"] for s in build_gate.visible_steps(job) if s["status"] == "completed" and s.get("conclusion") not in ("success", "skipped")]
        if failed:
            embed.add_field(name="Failed step", value=failed[0], inline=False)
    if request.run_url:
        embed.add_field(name="Run", value=f"[View on GitHub]({request.run_url})", inline=False)
    if request.commits and status in (build_gate.SUCCEEDED, build_gate.FAILED):
        embed.add_field(name="Commits (latest first)", value=commit_lines(request.commits, limit=4), inline=False)
    if note:
        embed.set_footer(text=note)
    return embed
