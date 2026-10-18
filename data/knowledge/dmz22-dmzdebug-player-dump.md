# DMZ v2.2 /dmzdebug player data dump

`/dmzdebug [ALL|STATS|CHARACTER|TECHNIQUES|QUESTS]` writes a JSON dump of your own DMZ data. Admins can run `/dmzdebug <player> [scope]` for someone else (needs dragonminez.admin or op). Non-admins have a short cooldown between uses.

The file is `dmzdebug/<player>.json`. When a player runs it, even on a remote server, the file is saved in that player's own game folder (the `.minecraft` or instance folder) and the chat message links to the folder. Run from the server console, it is saved in the server folder. In v2.2 the dump is JSON (`schemaVersion` 1) with party and quest blocks and the raw stat sections (Stats, BonusStats, Resources, Status, Cooldowns, Effects, Skills). v2.1 and older wrote `<player>.log` text instead.

Use it when reporting bugs about stats, forms, quests or progress: attach the file to the bug report or ticket. The support bot reads both formats.

Errors: "No stats data available" means the player data is not loaded yet (rejoin); "dump too large to send" means the data is unusually big, report it.

Keywords: dmzdebug, debug, debug file, player dump, bug report data, stats dump, quest progress dump, dmzdebug folder
