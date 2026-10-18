# DMZ v2.2 ki weapons, weapon movesets and weapon attributes (modded weapons)

Ki weapons need the Ki Manipulation skill: with an empty main hand, pick Ki Blade (sword moveset), Ki Scythe or Ki Claw Lance (trident) from Ki Weapons in the Utility menu. Bonus damage is Ki Damage x 0.25 / 0.45 / 0.65 x (Ki Manipulation level x 10%), costing Ki per attack. Tuning: `kiWeaponsConfig` in combat.json (`blade`, `scythe`, `clawlance`).

Every weapon (and bare fists) has a moveset: a combo chain with its own reach, sweep, windup and damage; dual-wielding alternates hands.

Weapon attributes are datapack files: `data/<namespace>/weapon_attributes/<item_path>.json` targets `<namespace>:<item_path>`. A file has `attributes` (range, animation, hitbox, damage multipliers, conditional attack variants, crit stats) or a `parent` to inherit from (cycles are skipped). `bettercombat:` namespaces are rewritten to `dragonminez:`, so most Better Combat weapon datapacks work. Reload with vanilla `/reload`, not /dmzreload.

Items without a file use combat.json rules: `fallbackCompatibilityEnabled` (true), `fallbackCompatibility` (`item_id_regex` to `weapon_attributes`, e.g. claymore to `dragonminez:claymore`), `blacklistItemIdRegex` ("pickaxe"), `weaponRegistryLogging` to log what each item resolved to.

Crafting from a template (Dr. Kikono's Station, `crafting` in general-server.json) can copy enchantments and, if enabled, Weapon Leveling and Apotheosis data.

Keywords: ki weapon, ki blade, ki scythe, weapon, modded weapon, weapon attributes, datapack, moveset, combo, fallback compatibility, blacklist, pickaxe, better combat datapack, apotheosis, kikono station
