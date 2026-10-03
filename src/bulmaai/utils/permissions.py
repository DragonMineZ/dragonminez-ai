from collections.abc import Sequence

import discord

from bulmaai.config import Settings, load_settings


def in_main_guild(member: discord.Member, settings: Settings) -> bool:
    """Commands are global, so a member object can come from any server the bot is in; only the main
    server (and the dev server, when set) may grant anything."""
    guild_id = getattr(getattr(member, "guild", None), "id", None)
    return guild_id is not None and guild_id in (settings.panel_guild_id, settings.dev_guild_id)


def is_admin(member: discord.Member, *, settings: Settings | None = None) -> bool:
    # DMs and user-installs hand us a discord.User, which has no guild_permissions.
    if not getattr(getattr(member, "guild_permissions", None), "administrator", False):
        return False
    return in_main_guild(member, settings or load_settings())


def has_any_allowed_role(member: discord.Member, role_ids: Sequence[int]) -> bool:
    allowed = {int(role_id) for role_id in role_ids}
    return any(r.id in allowed for r in getattr(member, "roles", []))


def is_staff(member: discord.Member, *, settings: Settings | None = None) -> bool:
    active_settings = settings or load_settings()
    staff_roles = set(active_settings.discord_staff_role_ids)
    for role in getattr(member, "roles", []):
        if role.id in staff_roles:
            return True
    return False


def has_patreon_access_role(
    member: discord.Member,
    *,
    settings: Settings | None = None,
) -> bool:
    active_settings = settings or load_settings()
    return has_any_allowed_role(member, active_settings.patreon_access_role_ids)


BRUNO_ID = 348174141121101824


def is_bruno(member: discord.Member) -> bool:
    return member.id == BRUNO_ID


def can_use_ai_support(
    member: discord.Member,
    *,
    settings: Settings | None = None,
) -> bool:
    active_settings = settings or load_settings()
    return (
        is_bruno(member)
        or is_staff(member, settings=active_settings)
        or has_any_allowed_role(member, active_settings.ai_support_allowed_role_ids)
    )
