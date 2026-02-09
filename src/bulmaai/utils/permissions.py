import discord

def is_admin(member: discord.Member) -> bool:
    return bool(getattr(member.guild_permissions, "administrator", False))

def has_any_allowed_role(member: discord.Member, role_ids: tuple[int, int]) -> bool:
    allowed = set(role_ids)
    return any(r.id in allowed for r in getattr(member, "roles", []))
