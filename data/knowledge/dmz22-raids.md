# DMZ v2.2 raids (Saiyan Assault, Frieza's Invasion, Gomah's Army, Red Ribbon Assault)

Raids are 5-wave battles you start by killing a marked scout (supervillain aura) at a raid site:
- Saiyan Assault: Saiyan Crater, Earth. Scout 500 HP. 10,000 TP. Defeat -10 alignment.
- Frieza's Invasion: Namekian Ruins, Namek. Scout 400 HP. 30,000 TP. Defeat -20.
- Gomah's Army: Gomah Camp, Demon Realm. Scout 10,000 HP. 350,000 TP. Defeat -30.
- Red Ribbon Assault: Red Ribbon base, Earth. Scout 150 HP (added in late v2.2 builds).
Wins also give Hero of the World (+25% TP for 10 minutes), golden carrots, senzu beans and an item.

Flow: within 80 blocks of a site you get a chat warning; each site has one scout and is spent once it dies. Killing it gives the Emperor's Wrath omen (60 seconds); removing the effect early (milk) cancels the raid. The raid starts where you stand; participants are you plus your party. Bosses stay dormant until their escort dies. Dying, logging out, changing dimension or going spectator takes you out; going over 80 blocks away starts a 15 second countdown. If nobody is left: YOU LOST! and the alignment penalty. Mob stats scale with the starter's story difficulty.

Config: `config/dragonminez/raids/<id>.json` (`enabled`, `leashDistance` 80, `trigger`, `waves`, `rewards`, `defeat`...). Extra files become new raids (`old_` ignored). `/dmzreload config`. Admin (op 2): `/dmzraid start [type]`, `stop`, `info`.

Keywords: raid, raids, saiyan assault, frieza invasion, gomah army, red ribbon, scout, emperor's wrath, waves, raid not starting, custom raid, dmzraid
