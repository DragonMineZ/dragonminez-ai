"""Known DragonMineZ addon mods and their minimum-required DMZ version.

Detection is by mod-id lookup against a static list (not a naming pattern),
so a mod only shows up as an "addon" once it's added here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# mod_id -> minimum DragonMineZ version the addon requires.
# ponytail: empty until the real addon list lands; check_addons is a no-op meanwhile.
KNOWN_ADDONS: dict[str, str] = {}

_RE_VERSION_PART = re.compile(r"\d+")


def _version_tuple(version: str) -> tuple[int, ...]:
    """Extract leading numeric parts, e.g. '1.20.1-3.2.0' -> (1, 20, 1, 3, 2, 0)."""
    return tuple(int(p) for p in _RE_VERSION_PART.findall(version))


@dataclass
class AddonStatus:
    mod_id: str
    version: str
    min_dmz_version: str
    compatible: bool | None  # None if the DMZ version itself wasn't detected


def check_addons(mods: dict[str, str], dmz_version: str | None) -> list[AddonStatus]:
    """Cross-reference detected mods against KNOWN_ADDONS and flag incompatibilities."""
    dmz_tuple = _version_tuple(dmz_version) if dmz_version else None
    results: list[AddonStatus] = []
    for mod_id, version in mods.items():
        min_version = KNOWN_ADDONS.get(mod_id)
        if min_version is None:
            continue
        compatible = dmz_tuple >= _version_tuple(min_version) if dmz_tuple else None
        results.append(AddonStatus(mod_id, version, min_version, compatible))
    return results
