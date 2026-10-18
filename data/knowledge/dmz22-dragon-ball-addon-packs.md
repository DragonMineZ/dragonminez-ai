# DMZ v2.2 Dragon Ball addon packs (custom ball sets, radars, dragons)

Packs go in the game folder: `<game dir>/dragonballs/` (on a server, next to the server jar). Each pack is a folder or a `.zip`. DMZ reads these files inside a pack:

- `definitions/ballset.json`: `id`, `dimensions`, `copies`, `spawn_range`, `summon_radius`, `blocks`, `asset_definition`, `display_name`.
- `definitions/radar.json`: `id`, `item_registry_name`, `dimensions`, `ball_set`, `tooltip_key`, `ranges`, `recipe_definition` or `chip_item` + `cpu_item` (auto recipe), `asset_definition`, `display_name`.
- `definitions/dragon.json`: `id`, `entity_registry_name`, `entity_width`, `entity_height`, `dimensions`, `ball_set`, `wish_screen_id`, `wish_count`, `asset_definition`.
- `definitions/wishes.json`: `dragon` and a `wishes` array (same wish types as world wish files).
- `assets/ballset.json`, `assets/radar.json`, `assets/dragon.json`: textures, models, animations.

Pack definitions merge with the built-in Earth, Namek and Demon sets. Problems (unknown keys, bad wish types) appear in the JSON load report. New blocks or items need a restart.

Keywords: dragon ball pack, addon pack, custom dragon balls, custom dragon, custom radar, dragonballs folder, ballset.json, dragon.json, zip pack
