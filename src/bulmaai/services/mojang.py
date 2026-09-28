from bulmaai.services.http import request

MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/{username}"


async def minecraft_username_exists(username: str) -> bool | None:
    """
    Look up `username` against Mojang's profile API.

    Returns True if it resolves to a real account, False if Mojang confirms it
    doesn't exist, or None if the lookup was inconclusive (network error, rate
    limit, unexpected status) - callers should treat None as "unknown", not
    "invalid", and fail open.
    """
    try:
        r = await request("GET", MOJANG_PROFILE_URL.format(username=username), timeout=10)
    except Exception:
        return None
    if r.status_code == 200:
        return True
    if r.status_code in (204, 404):
        return False
    return None
