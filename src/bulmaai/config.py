import os
from dataclasses import dataclass
from typing import Sequence


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int_list(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


# Frozen so read-only after creation
@dataclass(frozen=True)
class Settings:

    discord_token: str
    dev_guild_id: int | None
    log_level: str
    initial_extensions: Sequence[str]
    openai_key: str
    openai_model: str
    openai_support_model: str
    openai_support_reasoning_effort: str
    openai_support_max_output_tokens: int
    openai_vision_model: str
    openai_translation_model: str
    openai_embedding_model: str

    POSTGRES_DSN: str | None
    PGHOST: str
    PGPORT: int | None
    PGUSER: str
    PGPASSWORD: str
    PGDB: str

    GH_APP_ID: str | None
    GH_INSTALLATION_ID: str | None
    GH_APP_PRIVATE_KEY_PEM: str | None
    GITHUB_OWNER: str
    GITHUB_REPOS: Sequence[str]
    GITHUB_DEFAULT_REPO: str
    GITHUB_WHITELIST_REPO: str
    GITHUB_BASE_BRANCH: str
    GITHUB_WHITELIST_FILE_PATH: str

    PATREON_CREATOR_TOKEN: str | None
    PATREON_CAMPAIGN_ID: str | None
    ai_support_enabled: bool
    ai_ticket_category_id: int | None
    ai_general_channel_ids: Sequence[int]
    ai_support_history_limit: int
    ai_support_timeout_seconds: int
    support_response_cache_enabled: bool
    message_presets_path: str

    discord_staff_role_ids: Sequence[int] = (1352882775304175668, # DMZ Dev
                                             1309022450671161476, # DMZ Author
                                             1216431257660035132, # DMZ Owner
                                             1341595261960589343, # DMZ Helper
                                             1341596685339725885) # Staff role


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
    PGPORT = _get_env("PGPORT")
    PGUSER = _get_env("PGUSER")
    PGPASSWORD = _get_env("PGPASSWORD")
    PGDB = _get_env("PGDB")

    GH_APP_ID = _get_env("GH_APP_ID")
    GH_INSTALLATION_ID = _get_env("GH_INSTALLATION_ID")
    GH_APP_PRIVATE_KEY_PEM = _get_env("GH_APP_PRIVATE_KEY_PEM")
    GITHUB_OWNER = _get_env("GITHUB_OWNER", "DragonMineZ")
    GITHUB_REPOS_RAW = _get_env("GITHUB_REPOS")
    GITHUB_REPOS = tuple(r.strip() for r in GITHUB_REPOS_RAW.split(",") if r.strip())
    GITHUB_DEFAULT_REPO = _get_env("GITHUB_DEFAULT_REPO", "dragonminez")
    GITHUB_WHITELIST_REPO = _get_env("GITHUB_WHITELIST_REPO", ".github")
    GITHUB_BASE_BRANCH = _get_env("GITHUB_BASE_BRANCH", "main")
    GITHUB_WHITELIST_FILE_PATH = _get_env("GITHUB_WHITELIST_FILE_PATH", "allowed_betatesters.txt")

    PATREON_CREATOR_TOKEN = _get_env("PATREON_CREATOR_TOKEN")
    PATREON_CAMPAIGN_ID = _get_env("PATREON_CAMPAIGN_ID")

    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in."
        )

    dev_guild_raw = _get_env("DEV_GUILD_ID")
    dev_guild_id = int(dev_guild_raw) if dev_guild_raw else None

    log_level = _get_env("LOG_LEVEL", "INFO") or "INFO"

    initial_extensions = (
        "bulmaai.cogs.meta",
        "bulmaai.cogs.patreon_whitelist_flow",
        "bulmaai.cogs.ai_tickets",
        "bulmaai.cogs.github_cmds",
        "bulmaai.cogs.ai_ann_translation",
        "bulmaai.cogs.rules",
        "bulmaai.cogs.support_us",
        "bulmaai.cogs.log_parser",
        "bulmaai.cogs.patreon_announcements",
    )

    if not openai_key:
        raise RuntimeError("OPENAI_KEY is missing. Check env vars.")
    if not GH_APP_ID or not GH_INSTALLATION_ID or not GH_APP_PRIVATE_KEY_PEM:
        raise RuntimeError("GitHub App credentials are missing. Check env vars.")

    openai_model = _get_env("OPENAI_MODEL", "gpt-5-mini") or "gpt-5-mini"
    openai_support_model = _get_env("OPENAI_SUPPORT_MODEL", openai_model) or openai_model
    openai_support_reasoning_effort = _get_env("OPENAI_SUPPORT_REASONING_EFFORT", "low") or "low"
    openai_support_max_output_tokens = int(_get_env("OPENAI_SUPPORT_MAX_OUTPUT_TOKENS", "700") or "700")
    openai_vision_model = _get_env("OPENAI_VISION_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini"
    openai_translation_model = _get_env("OPENAI_TRANSLATION_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini"
    openai_embedding_model = _get_env("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large") or "text-embedding-3-large"

    ai_support_enabled = _parse_bool(_get_env("AI_SUPPORT_ENABLED"), default=False)
    ai_ticket_category_raw = _get_env("AI_TICKET_CATEGORY_ID")
    ai_ticket_category_id = int(ai_ticket_category_raw) if ai_ticket_category_raw else None
    ai_general_channel_ids = _parse_int_list(_get_env("AI_GENERAL_CHANNEL_IDS"))
    ai_support_history_limit = int(_get_env("AI_SUPPORT_HISTORY_LIMIT", "12") or "12")
    ai_support_timeout_seconds = int(_get_env("AI_SUPPORT_TIMEOUT_SECONDS", "45") or "45")
    support_response_cache_enabled = _parse_bool(_get_env("SUPPORT_RESPONSE_CACHE_ENABLED"), default=True)
    message_presets_path = _get_env("MESSAGE_PRESETS_PATH", "data/message_presets.json") or "data/message_presets.json"

    return Settings(
        discord_token=token,
        dev_guild_id=dev_guild_id,
        log_level=log_level,
        initial_extensions=initial_extensions,
        openai_key=openai_key,
        openai_model=openai_model,
        openai_support_model=openai_support_model,
        openai_support_reasoning_effort=openai_support_reasoning_effort,
        openai_support_max_output_tokens=openai_support_max_output_tokens,
        openai_vision_model=openai_vision_model,
        openai_translation_model=openai_translation_model,
        openai_embedding_model=openai_embedding_model,
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
        GITHUB_REPOS=GITHUB_REPOS,
        GITHUB_DEFAULT_REPO=GITHUB_DEFAULT_REPO,
        GITHUB_WHITELIST_REPO=GITHUB_WHITELIST_REPO,
        GITHUB_BASE_BRANCH=GITHUB_BASE_BRANCH,
        GITHUB_WHITELIST_FILE_PATH=GITHUB_WHITELIST_FILE_PATH,
        PATREON_CREATOR_TOKEN=PATREON_CREATOR_TOKEN,
        PATREON_CAMPAIGN_ID=PATREON_CAMPAIGN_ID,
        ai_support_enabled=ai_support_enabled,
        ai_ticket_category_id=ai_ticket_category_id,
        ai_general_channel_ids=ai_general_channel_ids,
        ai_support_history_limit=ai_support_history_limit,
        ai_support_timeout_seconds=ai_support_timeout_seconds,
        support_response_cache_enabled=support_response_cache_enabled,
        message_presets_path=message_presets_path,
    )
