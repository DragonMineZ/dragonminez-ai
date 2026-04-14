from collections.abc import Sequence

import discord

from bulmaai.config import Settings, load_settings


def is_admin(member: discord.Member) -> bool:
    return bool(getattr(member.guild_permissions, "administrator", False))


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


def is_bruno(member: discord.Member) -> bool:
    return member.id == 348174141121101824


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
