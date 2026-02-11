import os
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Settings:

    discord_token: str
    dev_guild_id: int | None
    log_level: str
    initial_extensions: Sequence[str]
    openai_key: str
    openai_model: str

    POSTGRES_DSN: str | None
    PGHOST: str
    PGPORT: int
    PGUSER: str
    PGPASSWORD: str
    PGDB: str

    GH_APP_ID: str | None
    GH_INSTALLATION_ID: str | None
    GH_APP_PRIVATE_KEY_PEM: str | None
    GITHUB_OWNER: str
    GITHUB_REPO: str
    GITHUB_BASE_BRANCH: str
    GITHUB_FILE_PATH: str


def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def load_settings() -> Settings:
    token = _get_env("DISCORD_TOKEN")
    openai_key = _get_env("OPENAI_KEY")

    PGDSN = _get_env("PGDSN")
    PGHOST = _get_env("PGHOST")
    PGPORT = int(_get_env("PGPORT", "5432") or "5432")
    PGUSER = _get_env("PGUSER")
    PGPASSWORD = _get_env("PGPASSWORD")
    PGDB = _get_env("PGDB")

    GH_APP_ID = _get_env("GH_APP_ID")
    GH_INSTALLATION_ID = _get_env("GH_INSTALLATION_ID")
    GH_APP_PRIVATE_KEY_PEM = _get_env("GH_APP_PRIVATE_KEY_PEM")
    GITHUB_OWNER = _get_env("GITHUB_OWNER", "DragonMineZ")
    GITHUB_REPO = _get_env("GITHUB_REPO", ".github")
    GITHUB_BASE_BRANCH = _get_env("GITHUB_BASE_BRANCH", "main")
    GITHUB_FILE_PATH = _get_env("GITHUB_FILE_PATH", "allowed_betatesters.txt")

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in."
        )

    dev_guild_raw = _get_env("DEV_GUILD_ID")
    dev_guild_id = int(dev_guild_raw) if dev_guild_raw else None

    log_level = _get_env("LOG_LEVEL", "INFO") or "INFO"

    initial_extensions = (
        "bulmaai.cogs.meta",
        # "bulmaai.cogs.faq", TODO: Re-enable when FAQ & Admin have code.
        "bulmaai.cogs.admin",
        "bulmaai.utils.patreon_whitelist",
        "bulmaai.cogs.ai_tickets",
    )

    if not openai_key:
        raise RuntimeError("OPENAI_KEY is missing. Talk to Bruno to fix.")

    openai_model = _get_env("OPENAI_MODEL")

    return Settings(
        discord_token=token,
        dev_guild_id=dev_guild_id,
        log_level=log_level,
        initial_extensions=initial_extensions,
        openai_key=openai_key,
        openai_model=openai_model,
        POSTGRES_DSN=PGDSN,
        PGHOST=PGHOST or "localhost",
        PGPORT=PGPORT,
        PGUSER=PGUSER,
        PGPASSWORD=PGPASSWORD,
        PGDB=PGDB,
        GH_APP_ID=GH_APP_ID,
        GH_INSTALLATION_ID=GH_INSTALLATION_ID,
        GH_APP_PRIVATE_KEY_PEM=GH_APP_PRIVATE_KEY_PEM,
        GITHUB_OWNER=GITHUB_OWNER,
        GITHUB_REPO=GITHUB_REPO,
        GITHUB_BASE_BRANCH=GITHUB_BASE_BRANCH,
        GITHUB_FILE_PATH=GITHUB_FILE_PATH,
    )
