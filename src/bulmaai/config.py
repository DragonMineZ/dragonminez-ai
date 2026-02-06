import os
from dataclasses import dataclass
from typing import Sequence

@dataclass(frozen=True)
class Settings:
    discord_token: str
    dev_guild_id: int | None
    log_level: str
    initial_extensions: Sequence[str]

def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def load_settings() -> Settings:
    token = _get_env("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")

    dev_guild_raw = _get_env("DEV_GUILD_ID")
    dev_guild_id = int(dev_guild_raw) if dev_guild_raw else None

    log_level = _get_env("LOG_LEVEL", "INFO") or "INFO"

    initial_extensions = (
        "bulmaai.cogs.meta",
        "bulmaai.cogs.faq",
        "bulmaai.cogs.admin",
    )

    return Settings(
        discord_token=token,
        dev_guild_id=dev_guild_id,
        log_level=log_level,
        initial_extensions=initial_extensions,
    )