# DMZ v2.2 mob and NPC stats (entities.json, saga enemies)

`config/dragonminez/entities.json` sets health, melee damage and ki damage for DMZ wildlife and generic enemies, keyed by entity id under `defaultEntityStats`: dinosaurs (dino kid 30 HP, dinosaurs 75 to 150 HP), sabertooth, giant fish, giant turtle, bandit (75 HP), Red Ribbon soldier (40 HP) and robots (120 HP), Mini Buu, red and blue ogres (200 HP), Mini Janemba stampede. Add more entity ids to override other DMZ mobs.

`transformDefaults`: enemies that can transform change at `triggerHealthPercent` 0.5 and gain x1.5 health, melee and ki multipliers.

Story and saga enemies (Raditz, Vegeta, Frieza, Cell, Buu...) get their stats from the quest objective that spawns them (`health`, `meleeDamage`, `kiDamage`, `AITier`, `CanTransform` in the quest JSON) and from difficulty multipliers. Raid and tournament fighters use their own JSON. Enemy AI tier (`aiTier`) controls how smart they fight (combos, dodging, ki use).

`accurateMobBattlePower` (combat.json) shows mob Battle Power from their real stats.

Keywords: mobs, mob stats, enemy health, enemy damage, entities.json, too strong, too weak, saga enemy stats, boss health, ai tier, transform health, dinosaur, ogre
