# DMZ v2.2 player data storage: NBT, JSON or MariaDB/MySQL database

DMZ player data (stats, race, forms, skills, quests...) is always saved in the normal Minecraft player file (NBT). `storage.storageType` in general-server.json adds a second, authoritative store:
- `NBT` (default): vanilla only.
- `JSON`: also `<world>/dragonminez/playerdata_json/<uuid>.json`.
- `DATABASE`: also a MariaDB/MySQL table, for sharing data across servers.

On login the extra store is loaded asynchronously and overwrites the vanilla copy. Saves are async and also run on auto-save.

Database settings (`storage` section): `host` (localhost), `port` (3306), `database` (dragonminez), `table` (player_data, created automatically), `username`, `password`, `poolSize` (10), `threadPoolSize` (4). Change the default username and password. The `storage` section is never sent to clients.

Log messages:
- "Database connected successfully!": OK.
- "DATABASE ERROR: Missing credentials" or "CRITICAL: Failed to connect to database" followed by "FALLBACK: ... NBT": the database is unreachable; data keeps saving to vanilla NBT only. Check host, port, firewall, user rights and that the database exists.
- "Could not load storage data for X; their storage record will not be overwritten this session": a load failed and DMZ protects the stored record.

`/dmzreload config` reloads storage settings (online players are saved first).

Keywords: storage, database, mysql, mariadb, sql, json storage, playerdata, data not saving, lost progress, shared data between servers, bungee, velocity, storageType, failed to connect to database
