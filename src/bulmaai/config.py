import json
import os
from collections.abc import Sequence as SequenceABC
from dataclasses import asdict, dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Sequence, Union, get_args, get_origin


def _require_env(name: str) -> str:
    value = _get_env(name)
    if value is None:
        raise RuntimeError(f"{name} is missing. Check .env secrets.")
    return value


def _get_env_int(name: str, default: int | None = None) -> int | None:
    value = _get_env(name)
    if value is None:
        return default
    return int(value)


def _get_env_bool(name: str, default: bool) -> bool:
    value = _get_env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _get_env_int_list(name: str, default: Sequence[int]) -> tuple[int, ...]:
    value = _get_env(name)
    if value is None:
        return tuple(default)
    parts = [part.strip() for part in value.split(",")]
    return tuple(int(part) for part in parts if part)


# Non-secret project config lives in source so `.env` stays secret-only.
DEFAULT_DEV_GUILD_ID: int | None = None
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_INITIAL_EXTENSIONS: Sequence[str] = (
    "bulmaai.cogs.meta",
    "bulmaai.cogs.patreon_whitelist_flow",
    "bulmaai.cogs.ai_tickets",
    "bulmaai.cogs.github_cmds",
    "bulmaai.cogs.ai_ann_translation",
    "bulmaai.cogs.rules",
    "bulmaai.cogs.support_us",
    "bulmaai.cogs.log_parser",
    "bulmaai.cogs.patreon_announcements",
    "bulmaai.cogs.curseforge_updates",
)

DEFAULT_OPENAI_MODEL = "gpt-5-mini"
# Ticket help uses tools, which are excluded from the data-sharing incentive program,
# so default to the higher-capability model there.
DEFAULT_OPENAI_SUPPORT_MODEL = "gpt-5.4"
DEFAULT_OPENAI_SUPPORT_REASONING_EFFORT = "medium"
DEFAULT_OPENAI_SUPPORT_MAX_OUTPUT_TOKENS = 1500
# Pin helper models to incentive-eligible IDs where possible.
DEFAULT_OPENAI_VISION_MODEL = "gpt-4.1-mini-2025-04-14"
DEFAULT_OPENAI_TRANSLATION_MODEL = "gpt-4.1-mini-2025-04-14"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-large"

DEFAULT_PGHOST = "localhost"
DEFAULT_PGPORT = 5432
DEFAULT_PGUSER = "dragonminez"
DEFAULT_PGDB = "dragonminez"

DEFAULT_GH_APP_ID = "2824274"
DEFAULT_GH_INSTALLATION_ID = "108888676"
DEFAULT_GITHUB_OWNER = "DragonMineZ"
DEFAULT_GITHUB_REPOS: Sequence[str] = (
    ".github",
    "dragonminez_ai",
    "dragonminez",
    "dragonminez-web",
)
DEFAULT_GITHUB_DEFAULT_REPO = "dragonminez"
DEFAULT_GITHUB_WHITELIST_REPO = ".github"
DEFAULT_GITHUB_BASE_BRANCH = "main"
DEFAULT_GITHUB_WHITELIST_FILE_PATH = "allowed_betatesters.txt"

DEFAULT_PATREON_CAMPAIGN_ID = "12861895"
DEFAULT_PATREON_WELCOME_CHANNEL_ID = 1287883800805642351
DEFAULT_PATREON_BOT_USER_ID = 216303189073461248

DEFAULT_AI_SUPPORT_ENABLED = True
DEFAULT_AI_TICKET_CATEGORY_ID = 1262517992982315110
DEFAULT_AI_GENERAL_CHANNEL_IDS: Sequence[int] = (
    1216429658459869195,
    1216430966667739198,
    1379205640387432569,
)
DEFAULT_AI_SUPPORT_HISTORY_LIMIT = 12
DEFAULT_AI_SUPPORT_TIMEOUT_SECONDS = 70
DEFAULT_AI_SUPPORT_TYPING_LEAD_SECONDS = 3
DEFAULT_AI_CLOSED_TICKET_CATEGORY_IDS: Sequence[int] = (1303543643377893466,)
DEFAULT_SUPPORT_RESPONSE_CACHE_ENABLED = True
DEFAULT_MESSAGE_PRESETS_PATH = "data/message_presets.json"
DEFAULT_ANNOUNCEMENT_SOURCE_CHANNEL_ID = 1260409720733175838
DEFAULT_ANNOUNCEMENT_SPANISH_CHANNEL_ID = 1280350384992288778
DEFAULT_ANNOUNCEMENT_PORTUGUESE_CHANNEL_ID = 1472964446866636892
DEFAULT_RELEASES_CHANNEL_ID = 1260409841424535624
DEFAULT_SNEAK_PEEKS_CHANNEL_ID = 1280350775989637130
DEFAULT_PATREON_ANNOUNCEMENT_CHANNEL_ID = 1490060558110822542
DEFAULT_BOT_RESTART_CHANNEL_ID = 1223439164121419838
DEFAULT_ANNOUNCEMENT_ROLE_EN_ID = 1260413114898317387
DEFAULT_ANNOUNCEMENT_ROLE_ES_ID = 1260413006202802276
DEFAULT_ANNOUNCEMENT_ROLE_PT_ID = 1469153940749680821
DEFAULT_CURSEFORGE_ENABLED = True
DEFAULT_CURSEFORGE_PROJECT_ID = 1136088
DEFAULT_CURSEFORGE_PROJECT_SLUG = "minecraft/mc-mods/dragonminez"
DEFAULT_CURSEFORGE_ANNOUNCEMENT_CHANNEL_ID = DEFAULT_RELEASES_CHANNEL_ID
DEFAULT_CURSEFORGE_POLL_MINUTES = 15
DEFAULT_SETTINGS_OVERRIDES_PATH = "data/settings_overrides.json"

NON_OVERRIDABLE_SETTINGS = {
    "discord_token",
    "openai_key",
    "POSTGRES_DSN",
    "PGPASSWORD",
    "GH_APP_PRIVATE_KEY_PEM",
    "PATREON_CREATOR_TOKEN",
    "curseforge_api_key",
}

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


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
    patreon_welcome_channel_id: int | None
    patreon_bot_user_id: int | None
    patreon_announcement_channel_id: int | None
    bot_restart_channel_id: int | None
    ai_support_enabled: bool
    ai_ticket_category_id: int | None
    ai_general_channel_ids: Sequence[int]
    ai_support_history_limit: int
    ai_support_timeout_seconds: int
    ai_support_typing_lead_seconds: int
    ai_closed_ticket_category_ids: Sequence[int]
    support_response_cache_enabled: bool
    message_presets_path: str
    announcement_source_channel_id: int | None
    announcement_spanish_channel_id: int | None
    announcement_portuguese_channel_id: int | None
    releases_channel_id: int | None
    sneak_peeks_channel_id: int | None
    announcement_role_en_id: int | None
    announcement_role_es_id: int | None
    announcement_role_pt_id: int | None
    curseforge_api_key: str | None
    curseforge_enabled: bool
    curseforge_project_id: int
    curseforge_project_slug: str
    curseforge_announcement_channel_id: int | None
    curseforge_poll_minutes: int

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


def _settings_overrides_path() -> Path:
    configured = Path(DEFAULT_SETTINGS_OVERRIDES_PATH)
    if configured.is_absolute():
        return configured
    return Path(__file__).resolve().parents[2] / configured


def _build_settings_from_env() -> Settings:
    token = _require_env("DISCORD_TOKEN")
    openai_key = _require_env("OPENAI_KEY")

    PGDSN = _get_env("PGDSN")
    PGPASSWORD = _get_env("PGPASSWORD")

    GH_APP_PRIVATE_KEY_PEM = _require_env("GH_APP_PRIVATE_KEY_PEM")

    PATREON_CREATOR_TOKEN = _get_env("PATREON_CREATOR_TOKEN")
    CURSEFORGE_API_KEY = _get_env("CURSEFORGE_API_KEY")

    return Settings(
        discord_token=token,
        dev_guild_id=DEFAULT_DEV_GUILD_ID,
        log_level=DEFAULT_LOG_LEVEL,
        initial_extensions=DEFAULT_INITIAL_EXTENSIONS,
        openai_key=openai_key,
        openai_model=_get_env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL) or DEFAULT_OPENAI_MODEL,
        openai_support_model=(
            _get_env("OPENAI_SUPPORT_MODEL", DEFAULT_OPENAI_SUPPORT_MODEL)
            or DEFAULT_OPENAI_SUPPORT_MODEL
        ),
        openai_support_reasoning_effort=(
            _get_env(
                "OPENAI_SUPPORT_REASONING_EFFORT",
                DEFAULT_OPENAI_SUPPORT_REASONING_EFFORT,
            )
            or DEFAULT_OPENAI_SUPPORT_REASONING_EFFORT
        ),
        openai_support_max_output_tokens=(
            _get_env_int(
                "OPENAI_SUPPORT_MAX_OUTPUT_TOKENS",
                DEFAULT_OPENAI_SUPPORT_MAX_OUTPUT_TOKENS,
            )
            or DEFAULT_OPENAI_SUPPORT_MAX_OUTPUT_TOKENS
        ),
        openai_vision_model=(
            _get_env("OPENAI_VISION_MODEL", DEFAULT_OPENAI_VISION_MODEL)
            or DEFAULT_OPENAI_VISION_MODEL
        ),
        openai_translation_model=(
            _get_env("OPENAI_TRANSLATION_MODEL", DEFAULT_OPENAI_TRANSLATION_MODEL)
            or DEFAULT_OPENAI_TRANSLATION_MODEL
        ),
        openai_embedding_model=(
            _get_env("OPENAI_EMBEDDING_MODEL", DEFAULT_OPENAI_EMBEDDING_MODEL)
            or DEFAULT_OPENAI_EMBEDDING_MODEL
        ),
        POSTGRES_DSN=PGDSN,
        PGHOST=DEFAULT_PGHOST,
        PGPORT=DEFAULT_PGPORT,
        PGUSER=DEFAULT_PGUSER,
        PGPASSWORD=PGPASSWORD,
        PGDB=DEFAULT_PGDB,
        GH_APP_ID=DEFAULT_GH_APP_ID,
        GH_INSTALLATION_ID=DEFAULT_GH_INSTALLATION_ID,
        GH_APP_PRIVATE_KEY_PEM=GH_APP_PRIVATE_KEY_PEM,
        GITHUB_OWNER=DEFAULT_GITHUB_OWNER,
        GITHUB_REPOS=DEFAULT_GITHUB_REPOS,
        GITHUB_DEFAULT_REPO=DEFAULT_GITHUB_DEFAULT_REPO,
        GITHUB_WHITELIST_REPO=DEFAULT_GITHUB_WHITELIST_REPO,
        GITHUB_BASE_BRANCH=DEFAULT_GITHUB_BASE_BRANCH,
        GITHUB_WHITELIST_FILE_PATH=DEFAULT_GITHUB_WHITELIST_FILE_PATH,
        PATREON_CREATOR_TOKEN=PATREON_CREATOR_TOKEN,
        PATREON_CAMPAIGN_ID=(
            _get_env("PATREON_CAMPAIGN_ID", DEFAULT_PATREON_CAMPAIGN_ID)
            or DEFAULT_PATREON_CAMPAIGN_ID
        ),
        patreon_welcome_channel_id=_get_env_int(
            "PATREON_WELCOME_CHANNEL_ID",
            DEFAULT_PATREON_WELCOME_CHANNEL_ID,
        ),
        patreon_bot_user_id=_get_env_int(
            "PATREON_BOT_USER_ID",
            DEFAULT_PATREON_BOT_USER_ID,
        ),
        patreon_announcement_channel_id=_get_env_int(
            "PATREON_ANNOUNCEMENT_CHANNEL_ID",
            DEFAULT_PATREON_ANNOUNCEMENT_CHANNEL_ID,
        ),
        bot_restart_channel_id=_get_env_int(
            "BOT_RESTART_CHANNEL_ID",
            DEFAULT_BOT_RESTART_CHANNEL_ID,
        ),
        ai_support_enabled=_get_env_bool("AI_SUPPORT_ENABLED", DEFAULT_AI_SUPPORT_ENABLED),
        ai_ticket_category_id=_get_env_int("AI_TICKET_CATEGORY_ID", DEFAULT_AI_TICKET_CATEGORY_ID),
        ai_general_channel_ids=_get_env_int_list("AI_GENERAL_CHANNEL_IDS", DEFAULT_AI_GENERAL_CHANNEL_IDS),
        ai_support_history_limit=_get_env_int("AI_SUPPORT_HISTORY_LIMIT", DEFAULT_AI_SUPPORT_HISTORY_LIMIT) or DEFAULT_AI_SUPPORT_HISTORY_LIMIT,
        ai_support_timeout_seconds=_get_env_int("AI_SUPPORT_TIMEOUT_SECONDS", DEFAULT_AI_SUPPORT_TIMEOUT_SECONDS) or DEFAULT_AI_SUPPORT_TIMEOUT_SECONDS,
        ai_support_typing_lead_seconds=_get_env_int("AI_SUPPORT_TYPING_LEAD_SECONDS", DEFAULT_AI_SUPPORT_TYPING_LEAD_SECONDS) or DEFAULT_AI_SUPPORT_TYPING_LEAD_SECONDS,
        ai_closed_ticket_category_ids=_get_env_int_list("AI_CLOSED_TICKET_CATEGORY_IDS", DEFAULT_AI_CLOSED_TICKET_CATEGORY_IDS),
        support_response_cache_enabled=_get_env_bool(
            "SUPPORT_RESPONSE_CACHE_ENABLED",
            DEFAULT_SUPPORT_RESPONSE_CACHE_ENABLED,
        ),
        message_presets_path=_get_env("MESSAGE_PRESETS_PATH", DEFAULT_MESSAGE_PRESETS_PATH) or DEFAULT_MESSAGE_PRESETS_PATH,
        announcement_source_channel_id=_get_env_int(
            "ANNOUNCEMENT_SOURCE_CHANNEL_ID",
            DEFAULT_ANNOUNCEMENT_SOURCE_CHANNEL_ID,
        ),
        announcement_spanish_channel_id=_get_env_int(
            "ANNOUNCEMENT_SPANISH_CHANNEL_ID",
            DEFAULT_ANNOUNCEMENT_SPANISH_CHANNEL_ID,
        ),
        announcement_portuguese_channel_id=_get_env_int(
            "ANNOUNCEMENT_PORTUGUESE_CHANNEL_ID",
            DEFAULT_ANNOUNCEMENT_PORTUGUESE_CHANNEL_ID,
        ),
        releases_channel_id=_get_env_int(
            "RELEASES_CHANNEL_ID",
            DEFAULT_RELEASES_CHANNEL_ID,
        ),
        sneak_peeks_channel_id=_get_env_int(
            "SNEAK_PEEKS_CHANNEL_ID",
            DEFAULT_SNEAK_PEEKS_CHANNEL_ID,
        ),
        announcement_role_en_id=_get_env_int(
            "ANNOUNCEMENT_ROLE_EN_ID",
            DEFAULT_ANNOUNCEMENT_ROLE_EN_ID,
        ),
        announcement_role_es_id=_get_env_int(
            "ANNOUNCEMENT_ROLE_ES_ID",
            DEFAULT_ANNOUNCEMENT_ROLE_ES_ID,
        ),
        announcement_role_pt_id=_get_env_int(
            "ANNOUNCEMENT_ROLE_PT_ID",
            DEFAULT_ANNOUNCEMENT_ROLE_PT_ID,
        ),
        curseforge_api_key=CURSEFORGE_API_KEY,
        curseforge_enabled=_get_env_bool("CURSEFORGE_ENABLED", DEFAULT_CURSEFORGE_ENABLED),
        curseforge_project_id=(
            _get_env_int("CURSEFORGE_PROJECT_ID", DEFAULT_CURSEFORGE_PROJECT_ID)
            or DEFAULT_CURSEFORGE_PROJECT_ID
        ),
        curseforge_project_slug=(
            _get_env("CURSEFORGE_PROJECT_SLUG", DEFAULT_CURSEFORGE_PROJECT_SLUG)
            or DEFAULT_CURSEFORGE_PROJECT_SLUG
        ),
        curseforge_announcement_channel_id=_get_env_int(
            "CURSEFORGE_ANNOUNCEMENT_CHANNEL_ID",
            DEFAULT_CURSEFORGE_ANNOUNCEMENT_CHANNEL_ID,
        ),
        curseforge_poll_minutes=(
            _get_env_int("CURSEFORGE_POLL_MINUTES", DEFAULT_CURSEFORGE_POLL_MINUTES)
            or DEFAULT_CURSEFORGE_POLL_MINUTES
        ),
    )


def load_settings_overrides() -> dict[str, Any]:
    path = _settings_overrides_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_settings_overrides(overrides: dict[str, Any]) -> None:
    path = _settings_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")


def get_editable_setting_names() -> tuple[str, ...]:
    names = [
        name
        for name in Settings.__annotations__
        if name not in NON_OVERRIDABLE_SETTINGS
    ]
    return tuple(sorted(names))


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin not in {Union, UnionType}:
        return annotation, False
    args = tuple(arg for arg in get_args(annotation) if arg is not type(None))
    if not args:
        return annotation, False
    if len(args) == 1:
        return args[0], True
    return annotation, False


def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ValueError("Expected a boolean value: true/false, yes/no, on/off, or 1/0.")


def _parse_sequence(value: str, *, item_type: type) -> tuple[Any, ...]:
    stripped = value.strip()
    if not stripped:
        return tuple()

    items: list[Any]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        items = parsed
    else:
        normalized = stripped.replace("\r", "\n")
        parts: list[str] = []
        for line in normalized.split("\n"):
            parts.extend(segment.strip() for segment in line.split(","))
        items = [part for part in parts if part]

    if item_type is int:
        return tuple(int(item) for item in items)
    return tuple(str(item).strip() for item in items if str(item).strip())


def _coerce_setting_value(field_name: str, raw_value: str) -> Any:
    annotation = Settings.__annotations__[field_name]
    target_type, allows_none = _unwrap_optional(annotation)
    lowered = raw_value.strip().lower()
    if allows_none and lowered in {"none", "null"}:
        return None

    origin = get_origin(target_type)
    if origin in {list, tuple, SequenceABC}:
        item_args = get_args(target_type)
        item_type = item_args[0] if item_args else str
        return _parse_sequence(raw_value, item_type=item_type)
    if target_type is bool:
        return _parse_bool(raw_value)
    if target_type is int:
        return int(raw_value.strip())
    return raw_value.strip()


def format_setting_value(value: Any) -> str:
    serializable = list(value) if isinstance(value, tuple) else value
    return json.dumps(serializable, ensure_ascii=False)


def set_setting_override(field_name: str, raw_value: str) -> Any:
    if field_name not in get_editable_setting_names():
        raise KeyError(field_name)
    overrides = load_settings_overrides()
    overrides[field_name] = _coerce_setting_value(field_name, raw_value)
    save_settings_overrides(overrides)
    return overrides[field_name]


def reset_setting_override(field_name: str) -> None:
    if field_name not in get_editable_setting_names():
        raise KeyError(field_name)
    overrides = load_settings_overrides()
    overrides.pop(field_name, None)
    save_settings_overrides(overrides)


def load_settings(*, include_overrides: bool = True) -> Settings:
    settings = _build_settings_from_env()
    if not include_overrides:
        return settings

    overrides = load_settings_overrides()
    merged = asdict(settings)
    for field_name in get_editable_setting_names():
        if field_name in overrides:
            merged[field_name] = overrides[field_name]
    return Settings(**merged)
