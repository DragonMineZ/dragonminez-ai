"""Components V2 cards for dev jars: the public download post and the staff Publish/Discard review card.
Buttons are built by the cog (they carry its custom ids) and passed in as one ActionRow."""

from datetime import datetime, timezone

import discord

from bulmaai.services.dev_jar_downloads import (
    DevJarArtifact,
    DevJarCommit,
    chunk_dev_jar_commit_lines,
    format_dev_jar_commit_line,
)

DEV_JAR_COLOR = discord.Colour.from_rgb(46, 204, 113)
DEV_JAR_REVIEW_COLOR = discord.Colour.blurple()
REVIEW_DONE_COLORS = {"Published": DEV_JAR_COLOR, "Discarded": discord.Colour.dark_grey()}
COMMITS_FILENAME = "dev-jar-commits.md"
# A V2 message holds 4000 characters of text in total.
WHATS_NEW_MAX = 2500
REVIEW_COMMITS_BUDGET = 2800
DOWNLOAD_FOOTER = "-# Downloads need Discord access authorization. Each link works once per user per jar."


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Unknown"
    return f"{size_bytes / (1024 * 1024):.3f} MB"


def _trim(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def artifact_day(artifact: DevJarArtifact) -> str:
    moment = artifact.modified_at or datetime.now(timezone.utc)
    return f"{moment:%B %d, %Y}"


def whats_new_container(changelog: str) -> discord.ui.Container:
    """The staff-written changelog, as the public post and the build gate preview both show it."""
    return discord.ui.Container(
        discord.ui.TextDisplay(f"### ✨ What's New\n{_trim(changelog, WHATS_NEW_MAX)}"), color=DEV_JAR_COLOR
    )


def _facts(artifact: DevJarArtifact, *, sha256: str | None, previous_size_bytes: int | None = None) -> str:
    size = _format_size(artifact.size_bytes)
    if previous_size_bytes is not None:
        size = f"{_format_size(previous_size_bytes)} → {size}"
    lines = [
        f"**Version** `{artifact.version}`　**Commit** `{artifact.commit_sha}`　**Size** {size}",
        f"**Artifact** `{artifact.file_name}`",
    ]
    if sha256:
        lines.append(f"**SHA-256** `{sha256}`")
    return "\n".join(lines)


def _run_link(workflow_run_url: str | None) -> str:
    return f" · [Workflow run](<{workflow_run_url}>)" if workflow_run_url else ""


def build_dev_jar_download_view(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    actions: discord.ui.ActionRow,
    changelog: str | None = None,
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    previous_size_bytes: int | None = None,
) -> tuple[discord.ui.DesignerView, str | None]:
    """The public post, plus the full commit list (attach it as COMMITS_FILENAME; the card links it)."""
    commit_list_text = "\n".join(format_dev_jar_commit_line(commit) for commit in commits) or None
    items: list[discord.ui.Item] = [
        discord.ui.TextDisplay(
            f"## 📦 DragonMineZ Dev Update\n"
            "A fresh dev build from the latest GitHub commits. Dev jars are untested and may be unstable or not "
            "start at all; stable beta/alpha releases are announced separately. "
            f"Press **Get download link** below.{_run_link(workflow_run_url)}"
        ),
        discord.ui.TextDisplay(_facts(artifact, sha256=sha256, previous_size_bytes=previous_size_bytes)),
        discord.ui.TextDisplay(f"**Patch Notes** for {artifact_day(artifact)} have everything that changed."),
    ]
    if commit_list_text:
        items.append(discord.ui.File(f"attachment://{COMMITS_FILENAME}"))
    items += [actions, discord.ui.TextDisplay(DOWNLOAD_FOOTER)]
    view = discord.ui.DesignerView(discord.ui.Container(*items, color=DEV_JAR_COLOR), timeout=None)
    if changelog and changelog.strip():
        view.add_item(whats_new_container(changelog))
    return view, commit_list_text


def build_dev_jar_review_view(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    status: str = "Pending review",
    actor: str | None = None,
    actions: discord.ui.ActionRow | None = None,
) -> tuple[discord.ui.DesignerView, str | None]:
    """The staff review card and, when the commits don't all fit, the full list to attach as COMMITS_FILENAME
    (only while actions are shown: a decided card drops its attachment)."""
    lines = [format_dev_jar_commit_line(commit) for commit in commits]
    chunks = chunk_dev_jar_commit_lines(lines, limit=REVIEW_COMMITS_BUDGET)
    overflow = len(chunks) > 1
    shown = chunks[0] if chunks else "No commits recorded."
    items: list[discord.ui.Item] = [
        discord.ui.TextDisplay(
            "## 🧪 DragonMineZ Dev Jar Review\n"
            "A push built and uploaded a dev jar. **Publish** announces it in the download channels, "
            f"**Discard** drops this build.{_run_link(workflow_run_url)}"
        ),
        discord.ui.TextDisplay(
            f"**Status** {status}　**Commits since last decision** {len(commits)}\n{_facts(artifact, sha256=sha256)}"
        ),
        discord.ui.TextDisplay(f"**Commits**\n{shown}"),
    ]
    if overflow:
        hidden = len(commits) - shown.count("\n") - 1
        if actions is not None:
            items.append(discord.ui.TextDisplay(f"-# {hidden} more not shown, see the attached list."))
            items.append(discord.ui.File(f"attachment://{COMMITS_FILENAME}"))
        else:
            items.append(discord.ui.TextDisplay(f"-# {hidden} more not shown."))
    if actor:
        items.append(discord.ui.TextDisplay(f"-# {actor}"))
    if actions is not None:
        items.append(actions)
    color = REVIEW_DONE_COLORS.get(status, DEV_JAR_REVIEW_COLOR)
    view = discord.ui.DesignerView(discord.ui.Container(*items, color=color), timeout=None)
    return view, ("\n".join(lines) if overflow and actions is not None else None)
