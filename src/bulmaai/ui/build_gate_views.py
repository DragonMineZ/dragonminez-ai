"""Embeds + buttons for the build gate. Buttons carry the request id in their custom_id and hold no state, so
they keep working after a restart (clicks are routed by the cog's on_interaction listener)."""

from datetime import datetime

import discord

from bulmaai.services import build_gate

APPROVE_PREFIX = "build_gate:approve:"
REJECT_PREFIX = "build_gate:reject:"

BLURPLE = 0x5865F2
GREEN = 0x57F287
RED = 0xED4245
GREY = 0x99AAB5
AMBER = 0xFEE75C
MAX_COMMIT_LINES = 8


def gate_view(request_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Build jar", style=discord.ButtonStyle.success, custom_id=f"{APPROVE_PREFIX}{request_id}"))
    view.add_item(discord.ui.Button(label="Skip", style=discord.ButtonStyle.secondary, custom_id=f"{REJECT_PREFIX}{request_id}"))
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


def gate_embed(request: build_gate.BuildRequest, *, recent: list[build_gate.BuildRequest], window_minutes: int) -> discord.Embed:
    embed = discord.Embed(
        title="🔨 Build a dev jar for this push?",
        description=f"{_header(request)}\nAuto-closes {_ts(request.expires_at)} if nobody answers.",
        color=BLURPLE,
    )
    embed.add_field(name="Commits (latest first)", value=commit_lines(request.commits), inline=False)
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
