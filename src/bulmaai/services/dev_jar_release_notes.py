import asyncio
import logging

from openai import AsyncOpenAI

from bulmaai.services.dev_jar_downloads import DevJarCommit, format_dev_jar_commit_line

log = logging.getLogger(__name__)

RELEASE_NOTES_TIMEOUT_SECONDS = 12.0
MAX_COMMITS_IN_PROMPT = 40
MAX_RELEASE_NOTES_CHARS = 900

RELEASE_NOTES_INSTRUCTIONS = """
You write short, player-facing "what's new" blurbs for DragonMineZ, a Minecraft Dragon Ball Z
mod, from a raw list of git commit titles/descriptions for an automated dev build. The reader
is a player deciding whether to grab this dev jar, not a developer.

Summarise the highlights in plain, exciting language. Group related changes together, skip
internal refactors/chores/CI/test-only commits unless there is nothing else notable, and never
invent features the commit list does not support. Keep it to a handful of short lines: no
changelog formatting, no commit hashes or links, no headers.
""".strip()


def _raw_commit_rendering(commits: tuple[DevJarCommit, ...]) -> str:
    """The pre-AI fallback: the raw commit list, one line per commit."""
    return "\n".join(format_dev_jar_commit_line(commit) for commit in commits)


def build_release_notes_prompt(commits: tuple[DevJarCommit, ...]) -> str:
    lines = []
    for commit in commits[:MAX_COMMITS_IN_PROMPT]:
        line = f"- {commit.title}"
        if commit.description:
            line += f": {commit.description}"
        lines.append(line)
    return "\n".join(lines)[:6000]


async def generate_dev_jar_release_notes(
    commits: tuple[DevJarCommit, ...],
    *,
    client: AsyncOpenAI | None,
    model: str,
    timeout_seconds: float = RELEASE_NOTES_TIMEOUT_SECONDS,
) -> str:
    """Short AI "what's new" blurb for the public dev jar announcement.

    Never raises and never blocks the announcement: on a missing client, an
    empty commit list, any OpenAI failure, a timeout, or an empty response,
    falls back to the raw commit list rendering used before this existed.
    """
    fallback = _raw_commit_rendering(commits)
    if client is None or not commits:
        return fallback

    request_kwargs: dict = {
        "model": model,
        "instructions": RELEASE_NOTES_INSTRUCTIONS,
        "input": build_release_notes_prompt(commits),
        "max_output_tokens": 600,
    }
    if model.startswith("gpt-5"):
        request_kwargs["reasoning"] = {"effort": "low"}

    try:
        response = await asyncio.wait_for(
            client.responses.create(**request_kwargs),
            timeout=timeout_seconds,
        )
    except Exception:
        log.warning("Dev jar release notes generation failed; using raw commit list", exc_info=True)
        return fallback

    text = (getattr(response, "output_text", None) or "").strip()
    if not text:
        log.warning("Dev jar release notes generation returned empty output; using raw commit list")
        return fallback
    return text[:MAX_RELEASE_NOTES_CHARS]
