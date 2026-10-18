# DMZ v2.2 combat logout, knockdowns, who you can hit, griefing gamerules

Combat logout: with `killPlayersOnCombatLogout` true (combat.json, default), logging out mid-fight kills you the next time you log in.

Knockdowns: in fights that don't allow kills, a hit that would kill a player knocks them down for 30 seconds instead (`knockdownDurationSeconds`); the K.O. effect shows the time left. This happens in party PvP, with Friendly Fist on (a Ki Action in the Utility menu), when sparring a Shadow Dummy, and while a Namekian or Majin has Assimilation/Absorption selected. World boss fights have their own knockout rules.

Who you can hit: party members are friendly unless the party has PvP on (`/dmzparty pvp`); players fighting the same world boss stay friendly; tournament rivals are always hostile. By default other players and mobs are hostile targets, except villagers, iron golems and Guard Villagers guards (neutral). `playerRelationTo*` in combat.json changes this.

Ki griefing gamerules (`/gamerule`): `allowKiGriefingPlayers` (true), `allowKiGriefingMobs` (true), `allowKiGriefingMasterStructures` (false, protects master houses), `allowBuildingInArenaStructures` (false). WorldGuard regions on hybrid servers also block ki block damage.

Keywords: combat log, combat logout, killed on login, died after relog, knockdown, knocked down, ko, friendly fist, pvp, friendly fire, gamerule, ki griefing, explosions break blocks, allowKiGriefingPlayers, protect structures
