"""Which language to answer someone in, and the per-language text tables.

A table is {language: {key: text}} with every key in every language (tests/test_i18n.py checks it). Adding a
language means a new entry in LANGUAGES / DISCORD_LOCALES, its role setting, and a new column in every table."""

from collections.abc import Mapping

from bulmaai.utils.language import role_languages

LANGUAGES = ("en", "es", "pt")
# Discord client locales that get each language's command descriptions (English is the default).
DISCORD_LOCALES = {"es": ("es-ES", "es-419"), "pt": ("pt-BR",)}

TABLES: list[Mapping[str, Mapping[str, object]]] = []


def table(texts: dict[str, dict]) -> dict[str, dict]:
    """Registers a per-language table so the completeness test sees it."""
    TABLES.append(texts)
    return texts


def t(texts: Mapping[str, Mapping[str, str]], key: str, language: str | None, **values) -> str:
    """texts[language][key], English when the language or the key is missing; formatted when values are given."""
    text = texts.get(language or "en", {}).get(key)
    if text is None:
        text = texts["en"][key]
    return text.format(**values) if values else text


def locale_language(locale) -> str | None:
    """es-ES / es-419 → es, pt-BR → pt, en-US / en-GB → en; None for a locale we don't support."""
    code = str(getattr(locale, "value", locale) or "").split("-")[0].lower()
    return code if code in LANGUAGES else None


def _main_member(user, settings, client):
    """user as a member of the main server: itself when it is one, else looked up in the cache; user itself when the
    lookup can't be made (its roles, if any, simply match no language role elsewhere)."""
    guild_id = getattr(settings, "panel_guild_id", None)
    if getattr(getattr(user, "guild", None), "id", None) == guild_id:
        return user
    get_guild = getattr(client, "get_guild", None)
    guild = get_guild(guild_id) if get_guild is not None and guild_id else None
    get_member = getattr(guild, "get_member", None)
    member = get_member(user.id) if get_member is not None else None
    return member if member is not None else user


def _client(interaction):
    """The bot behind an Interaction (.client) or an ApplicationContext (.bot)."""
    if interaction is None:
        return None
    return getattr(interaction, "client", None) or getattr(interaction, "bot", None)


def user_language(user, settings, interaction=None, *, client=None) -> str:
    """The language to answer user in: their single language role wins; with several, the interaction's Discord
    locale if it is one of them, else the first in en, es, pt order; with none, the locale if we support it, else
    English. Roles are read off their main-server member (DMs and other servers included)."""
    client = client or _client(interaction)
    member = _main_member(user, settings, client) if user is not None and settings is not None else None
    roles = role_languages(member, settings) if member is not None else []
    locale = locale_language(getattr(interaction, "locale", None)) if interaction is not None else None
    if len(roles) == 1:
        return roles[0]
    if roles:
        return locale if locale in roles else roles[0]
    return locale or "en"


def reader_language(interaction, settings=None) -> str:
    """user_language for whoever triggered interaction (an Interaction or an ApplicationContext; both carry .user and
    .locale). settings: the bot's own when not given."""
    settings = settings if settings is not None else getattr(_client(interaction), "settings", None)
    return user_language(getattr(interaction, "user", None), settings, interaction)


def localizations(texts: Mapping[str, Mapping[str, str]], key: str) -> dict[str, str]:
    """Discord description_localizations for texts[key]: every supported non-English client locale."""
    return {locale: texts[language][key] for language, locales in DISCORD_LOCALES.items() for locale in locales}


def described(texts: Mapping[str, Mapping[str, str]], key: str) -> dict[str, object]:
    """description= and description_localizations= kwargs for a command or option."""
    return {"description": texts["en"][key], "description_localizations": localizations(texts, key)}
