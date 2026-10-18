# DMZ v2.2 world bosses: lairs and how the fight works

Lairs: Janemba (bottom of Hell, Otherworld), Turles (under the Tree of Might, swamps, Earth), Metal Cooler Core (inner chamber of the Big Gete Star, Namek), Tamagami 3, 2 and 1 (platforms on the Third, Second and First Demon World), Gomah (Horn Cradle above the First Demon World). Keys: janemba, turles, metal_cooler_core, tamagami_3, tamagami_2, tamagami_1, gomah. Maps: Hell Ogre (Janemba), Namekian Traders (Gete Star), Time Patroller (Tamagami, Gomah).

The boss spawns when a player is within 128 blocks and wakes within 32. If nobody is fighting it sleeps again at full HP. Respawn after 1 hour of game time; lairs repair themselves. Bosses are tuned like Hard difficulty with Expert AI.

Fight: everyone within 150 blocks joins and fighters can't hurt each other (truce until 15 seconds after). Each extra fighter adds +50% HP and +25% damage. Lethal hits knock you out instead of killing; 3 lives each. A Revive technique is put in technique slot 4: look at a downed ally within 6 blocks and hold still 15 seconds (moving, stuns or 10% HP damage cancel); restores 25%, 90 second cooldown. If everyone is down at once the fight is lost.

Admin (op 2): `/dmzworldboss locate [boss]`, `tp [boss]`, `reset [boss]`, `ready [boss]`. Config: `worldBoss` in general-server.json.

Keywords: world boss, boss, janemba, turles, metal cooler, gomah, tamagami, lair, where is the boss, boss respawn, boss asleep, revive teammate, lives, knocked out, dmzworldboss locate
