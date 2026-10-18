# DMZ v2.2 Dragon Balls: sets, spawning, radar, summoning

- Earth: 7 balls, summons Shenron on Earth (1 wish; revive wish revives up to 3 dead players).
- Namek: 7 balls, summons Porunga on Namek (3 wishes; 1 revive per pick).
- Demon Realm: 3 balls, Porunga (Daima) in the Demon Realm (3 wishes). Never scattered and no radar: each Tamagami world boss drops one (3, 2 and 1 star), and they don't scatter after the wish.

Spawning: Earth and Namek balls scatter around that world's spawn within `worldGen.dbSpawnRange` (1000), with `dragonBallSets` copies (1). Every ball is tracked; a lost ball respawns. `generateDragonBalls` false disables it.

Radars: Dragon Radar (Earth) crafted from Radar's Chip and Radar's CPU (Earth), also a GT Saga reward. Dragon Radar (Namek) from the Namek chip and CPU. Bi-Dimensional Radar (both worlds) is a quest reward fused by Bulma; not craftable.

Summoning: place the full set within 5 blocks of each other in its world (they glow and chime), right-click one. Only the summoner picks wishes (left-click pick, right-click remove). The sky darkens while the dragon is out. With no wish in 10 minutes it leaves. The balls scatter again afterwards.

Admin: `/dmzdragonballs status [set]`, `/dmzdragonballs regenerate <set>` (refill to the configured copies).

Keywords: dragon balls, dragonballs, shenron, porunga, summon, how to summon, radar, dragon radar, bi-dimensional radar, cant find dragon balls, demon realm dragon balls, dbSpawnRange, scatter, regenerate, dmzdragonballs
