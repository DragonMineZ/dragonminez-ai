# DMZ v2.2 compatibility with other mods

Required: GeckoLib, TerraBlender, Curios API. DMZ wearables use Curios slots: scouters (red, blue, green, purple), Potara earrings and training weights.

Incompatible (blocked at startup): Epic Fight, Better Combat, Legendary Tooltips.

Built-in integrations (work automatically when the other mod is present):
- JEI: recipe categories for the Kikono Station and Pattern Station.
- Apotheosis: crafting from a template can keep rarity, affixes, sockets and gems (toggles in `crafting`, general-server.json).
- Weapon Leveling and AutoLeveling: weapon level and progress can carry over when crafting from a template.
- Cosmetic Armor Reworked: cosmetic armor slots are respected when rendering DMZ armor layers.
- WorldGuard (only on hybrid Forge+Bukkit servers like Mohist or Arclight): DMZ respects region block protection for ki damage and adds a `dmz-gravity` region flag.

Modded weapons: DMZ gives them combat behavior through `fallbackCompatibility` rules in combat.json.

Other combat, animation or player-model overhaul mods can conflict because DMZ replaces the player model, animations and damage. Test with only DMZ and its dependencies first, then add mods back to find the conflict.

Keywords: compatibility, compatible, incompatible, mod conflict, curios, jei, apotheosis, weapon leveling, cosmetic armor, worldguard, mohist, arclight, epic fight, better combat, modpack, crash with other mod
