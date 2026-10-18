# DMZ v2.2 server and world commands

- `/dmzreload [all|config|story|wishes]`: reload and resync data.
- `/dmzconfig <file> [section] <key> <value>`: edit a config value in game (admin), e.g. `/dmzconfig general-server gameplay tpPerHit 5` (space, not a dot).
- `/dmzrestoreupdate` then `confirm`: delete and regenerate the world's wishes, sagas and quests folders (admin).
- `/dmzquest list|info|start|finish|fail|reset|track <quest> [targets]`, `startsaga|finishsaga|resetsaga <saga>`, `difficulty <difficulty>`, `questnpc spawn|remove`.
- `/dmzparty invite|accept|confirm|reject|leave|list|kick|disband|pvp`.
- `/dmzdragonballs status [set]`, `/dmzdragonballs regenerate <set>`.
- `/dmzlocate <structure>`, `/dmzstructure here|npcs`.
- `/dmzraid start [type]|stop|info` (op), `/dmztournament ring <id>|cooldown clear <players> [tournament]` (op).
- `/dmzworldboss results` (everyone); `locate [boss]`, `tp [boss]`, `reset [boss]`, `ready [boss]` (op).
- `/dmzracial reply accept|reject`: answer a Namekian assimilation request (no permission).
- `/dmzminigame play|learn <minigame> [targets]`.
- `/dmzweight turtle_shell|workout_weights|piccolo_cape <weight>`: give weight items.
- `/dmzdebug [scope]` or `/dmzdebug <player> [scope]`: data dump.

`commandOutputOnConsole` (general-server gameplay) controls whether command results are logged to the console. Gamerules: allowKiGriefingPlayers, allowKiGriefingMobs, allowKiGriefingMasterStructures, allowBuildingInArenaStructures.

Keywords: commands, server commands, dmzreload, dmzquest, dmzparty, dmzdragonballs, dmzlocate, dmzraid, dmzworldboss, dmztournament, dmzminigame, dmzweight, dmzdebug, list of commands
