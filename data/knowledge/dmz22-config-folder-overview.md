# DMZ v2.2 config files: what lives in config/dragonminez

All configs are JSON in `config/dragonminez/` (inside the server or instance folder). They regenerate automatically if deleted.

- `general-server.json`: gameplay, TP, world gen, racial skills, gravity, storage (database), fusion, party, world boss, hair limits.
- `general-user.json`: client-only visual and HUD options. Never synced; each player keeps their own.
- `hud_layout.json`: client-only HUD positions (edit in-game with the HUD editor).
- `combat.json`: damage mitigation, blocking, parry, speed and dodge, dash, combat flight, ki surge, ki weapons.
- `training.json`: training minigame tuning and TP rewards.
- `skills.json`: skill TP costs per level, allowed races, what each master teaches.
- `techniques.json`: ki, strike and evasion technique multipliers, XP, cooldowns.
- `entities.json`: health and damage of vanilla-style DMZ mobs.
- `races/<race>/character.json`, `stats.json`, `forms/*.json`: per race.
- `forms/*.json`: stack forms (Kaioken, Ultimate).
- `raids/*.json`, `tournaments/*.json`: raid and tournament definitions.

On a multiplayer server the server's files win: every file except general-user and hud_layout is sent to each client on join and after `/dmzreload`. Editing configs on the client does nothing on a server.

Keywords: config, configs, folder, general-server.json, combat.json, skills.json, techniques.json, entities.json, training.json, races, forms, where are the configs, server config sync
