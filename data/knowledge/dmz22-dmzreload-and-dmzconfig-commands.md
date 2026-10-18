# DMZ v2.2 /dmzreload, /dmzconfig, /reload and what needs a restart

`/dmzreload [all|config|story|wishes]` (dragonminez.dmz.reload, dragonminez.admin or op 2; console always):
- `config`: every file in config/dragonminez (general, combat, training, skills, techniques, entities, races, forms, raids, tournaments), raid types, storage backend, NPC alignment rules and NPC placements. Then configs are resent to everyone.
- `story`: sagas, quests, side quests (runs the quest auto-upgrade) and NPC dialogues, resent to everyone.
- `wishes`: world wish files, resent to everyone.
- `all` (default): everything. Players don't need to relog.

`/dmzconfig <file> [section] <key> <value>` (admin or op 2): changes one plain value, saves and resends, e.g. `/dmzconfig general-server gameplay tpGainMultiplier 2.0`. Lists and deeper objects must be edited in the file.

The Forge Mods screen has a DragonMineZ config screen (edits your own game folder; for singleplayer and LAN). On a dedicated server, edit the server files and reload. Edits without a reload only reach players who join later.

Not covered by /dmzreload:
- general-user.json and hud_layout.json (client files).
- Resource assets: F3+T on the client.
- Datapack data (`spacepod/`, `weapon_attributes/`, `rhythm_charts/`) and Dragon Ball packs in `<game dir>/dragonballs/`: vanilla `/reload` (then `/dmzreload wishes`).
- New jars or code: restart.

Keywords: dmzreload, reload, reload config, dmzconfig, edit config in game, apply changes, resync, restart required, vanilla reload, config screen
