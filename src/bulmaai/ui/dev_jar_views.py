from collections.abc import Iterable
from datetime import datetime, timezone

import discord

from bulmaai.services.dev_jar_downloads import (
    DevJarArtifact,
    DevJarCommit,
    DevJarCommitLayout,
    build_dev_jar_commit_layout,
)

DEV_JAR_EMBED_COLOR = discord.Colour.from_rgb(46, 204, 113)
DEV_JAR_REVIEW_EMBED_COLOR = discord.Colour.blurple()
# Discord caps embeds at 25 fields; leave headroom for trailing fields appended
# after the commit changelog (e.g. Patch Notes).
MAX_FIELDS_PER_EMBED = 24
# Discord caps a single embed field value at 1024 characters.
MAX_FIELD_VALUE_CHARS = 1024


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Unknown"
    size_mb = size_bytes / (1024 * 1024)
    return f"{size_mb:.3f} MB"


def _truncate_field(text: str, limit: int = MAX_FIELD_VALUE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def artifact_day(artifact: DevJarArtifact) -> str:
    moment = artifact.modified_at or datetime.now(timezone.utc)
    return f"{moment:%B %d, %Y}"


def _build_base_dev_jar_fields(
    artifact: DevJarArtifact,
    *,
    sha256: str | None,
    previous_size_bytes: int | None,
) -> list[tuple[str, str, bool]]:
    fields: list[tuple[str, str, bool]] = [
        ("Version", f"`{artifact.version}`", True),
        ("Commit .jar", f"`{artifact.commit_sha}`", True),
        ("Artifact", f"`{artifact.file_name}`", False),
    ]
    size_str = _format_size(artifact.size_bytes)
    if previous_size_bytes is not None:
        size_str = f"{_format_size(previous_size_bytes)} → {size_str}"
    fields.append(("Size", size_str, True))
    if sha256:
        fields.append(("SHA-256", f"`{sha256}`", False))
    return fields


def _append_fields_across_embeds(
    embeds: list[discord.Embed],
    fields: Iterable[tuple[str, str]],
    *,
    colour: discord.Colour,
    inline: bool = False,
) -> None:
    """Append (name, value) fields to the last embed, spilling into new embeds
    once the 25-fields-per-embed limit is hit."""
    current = embeds[-1]
    current_field_count = len(current.fields)
    for name, value in fields:
        if current_field_count >= MAX_FIELDS_PER_EMBED:
            current = discord.Embed(colour=colour)
            embeds.append(current)
            current_field_count = 0
        current.add_field(name=name, value=value, inline=inline)
        current_field_count += 1


def _build_changelog_embeds(
    descriptions: Iterable[str],
    *,
    colour: discord.Colour,
) -> list[discord.Embed]:
    """One embed per changelog chunk. Only the first carries the
    "Commits Changelog" title; continuation chunks are untitled."""
    return [
        discord.Embed(
            title="Commits Changelog" if index == 0 else None,
            description=text,
            colour=colour,
        )
        for index, text in enumerate(descriptions)
    ]


def _build_dev_jar_embeds(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    title: str,
    description: str,
    colour: discord.Colour,
    sha256: str | None,
    previous_size_bytes: int | None = None,
    workflow_run_url: str | None = None,
    timestamp: datetime | None = None,
    leading_fields: tuple[tuple[str, str, bool], ...] = (),
    trailing_fields: tuple[tuple[str, str], ...] = (),
    footer_text: str | None = None,
    show_commit_changelog: bool = True,
) -> tuple[list[discord.Embed], DevJarCommitLayout]:
    """Shared body for the public download and staff review dev jar embeds.

    Always computes the commit layout (needed for the full changelog text
    handed back to the caller for the commit-list file attachment) but only
    renders it as changelog embeds when `show_commit_changelog` is set.
    """
    base_fields = _build_base_dev_jar_fields(
        artifact, sha256=sha256, previous_size_bytes=previous_size_bytes
    )
    static_char_count = (
        len(title)
        + len(description)
        + len(footer_text or "")
        + sum(len(name) + len(value) for name, value in trailing_fields)
        + sum(len(name) + len(value) for name, value, _ in leading_fields)
        + sum(len(name) + len(value) for name, value, _ in base_fields)
    )
    layout = build_dev_jar_commit_layout(commits, base_char_count=static_char_count)

    primary = discord.Embed(
        title=title,
        description=description,
        url=workflow_run_url,
        colour=colour,
        timestamp=timestamp,
    )
    for name, value, inline in (*leading_fields, *base_fields):
        primary.add_field(name=name, value=value, inline=inline)

    embeds = [primary]
    if show_commit_changelog:
        embeds.extend(_build_changelog_embeds(layout.descriptions, colour=colour))
    if trailing_fields:
        _append_fields_across_embeds(embeds, trailing_fields, colour=colour)
    if footer_text:
        embeds[-1].set_footer(text=footer_text)

    return embeds, layout


def build_dev_jar_download_embeds(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    release_notes: str = "",
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    previous_size_bytes: int | None = None,
) -> tuple[list[discord.Embed], str | None]:
    """Build the public dev jar announcement embed(s).

    `release_notes` is the short player-facing "what's new" blurb (AI-written,
    or the raw-commit-rendering fallback — see dev_jar_release_notes.py); the
    full commit list is never rendered inline here, only handed back as the
    second return value so the caller can always attach it as a file.
    """
    description = (
        "A push has been detected in GitHub and a .jar has successfully passed tests and is ready to be "
        "downloaded! These versions are automatically built by the latest commits, meaning they can be "
        "unstable or not run at all on your machine. For stable (and mostly tested) beta/alpha releases, "
        "look for them in Discord. Click the button to download the latest dev jar, and see What's New "
        "below for a summary of what changed (the full commit list is attached)."
    )
    footer_text = "Downloads require Discord access authorization. Download links are one-time per user per jar."
    notes_day = artifact_day(artifact)
    patch_notes_value = (
        f"The **Patch Notes** button below opens the patch notes for {notes_day} "
        "with everything that changed in this update."
    )
    whats_new_value = _truncate_field(release_notes.strip() or "No changes recorded.")

    embeds, layout = _build_dev_jar_embeds(
        artifact,
        commits=commits,
        title="DragonMineZ Dev Update",
        description=description,
        colour=DEV_JAR_EMBED_COLOR,
        sha256=sha256,
        previous_size_bytes=previous_size_bytes,
        workflow_run_url=workflow_run_url,
        timestamp=artifact.modified_at,
        trailing_fields=(("What's New", whats_new_value), ("Patch Notes", patch_notes_value)),
        footer_text=footer_text,
        show_commit_changelog=False,
    )
    return embeds, (layout.full_changelog_text or None)


def build_dev_jar_download_embed(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    release_notes: str = "",
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    previous_size_bytes: int | None = None,
) -> discord.Embed:
    """Convenience wrapper returning just the primary embed (single-embed case)."""
    embeds, _ = build_dev_jar_download_embeds(
        artifact,
        commits=commits,
        release_notes=release_notes,
        sha256=sha256,
        workflow_run_url=workflow_run_url,
        previous_size_bytes=previous_size_bytes,
    )
    return embeds[0]


def build_dev_jar_review_embeds(
    artifact: DevJarArtifact,
    *,
    commits: tuple[DevJarCommit, ...],
    sha256: str | None = None,
    workflow_run_url: str | None = None,
    status: str = "Pending review",
    actor: str | None = None,
) -> tuple[list[discord.Embed], str | None]:
    """Build the staff review embed(s) posted to the dev jar review channel."""
    description = (
        "A push has been detected on GitHub and the dev jar built and uploaded successfully. "
        "Review the accumulated commits below, then **Publish** to announce it to the download "
        "channels, or **Discard** to drop this build without publishing."
    )
    leading_fields = (
        ("Status", status, True),
        ("Commits since last decision", str(len(commits)), True),
    )

    embeds, layout = _build_dev_jar_embeds(
        artifact,
        commits=commits,
        title="DragonMineZ Dev Jar Review",
        description=description,
        colour=DEV_JAR_REVIEW_EMBED_COLOR,
        sha256=sha256,
        workflow_run_url=workflow_run_url,
        leading_fields=leading_fields,
        footer_text=actor,
    )
    overflow_text = layout.full_changelog_text if layout.overflowed else None
    return embeds, overflow_text
