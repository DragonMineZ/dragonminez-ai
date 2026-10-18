# DMZ v2.2 world-save folders (world/dragonminez)

Per-world data lives in `<world>/dragonminez/`:
- `sagas/`: saga manifests (classic, saiyan, frieza, android, future, buu, movies, gt, daima).
- `quests/<saga folder>/`: the quests of each saga (for example `quests/saga_saiyan/`).
- `sidequests/<category>/`: side quests.
- `wishes/*.json`: wish lists, one file per dragon (shenron.json, porunga.json).
- `dialogues/`: NPC dialogue trees (an `_example.json` is written).
- `npcs/placements.json` and `npcs/alignment_rules.json`: NPC spawn placements and alignment interaction rules.
- `playerdata_json/<uuid>.json`: player data when storage type is JSON.
- `oldBackup/`: originals of quest files that were auto-upgraded.
- `quest_update_report.txt`: what the last quest upgrade changed.

Global (not per world): `config/dragonminez/` for configs, `<game dir>/dragonballs/` for dragon ball addon packs, `<game dir>/dmzdebug/` for /dmzdebug dumps.

These files are meant to be edited by server owners. Back them up before big changes.

Keywords: world folder, save folder, world/dragonminez, where are quests stored, wishes folder, sagas folder, playerdata_json, dialogues folder, npcs placements
