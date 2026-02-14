import discord

from bulmaai.config import load_settings

settings = load_settings()


def is_admin(member: discord.Member) -> bool:
    return bool(getattr(member.guild_permissions, "administrator", False))


def has_any_allowed_role(member: discord.Member, role_ids: tuple[int, int]) -> bool:
    allowed = set(role_ids)
    return any(r.id in allowed for r in getattr(member, "roles", []))


def is_staff(member: discord.Member) -> bool:
    staff_roles = set(settings.discord_staff_role_ids)
    for role in getattr(member, "roles", []):
        if role.id in staff_roles:
            return True
    return False
