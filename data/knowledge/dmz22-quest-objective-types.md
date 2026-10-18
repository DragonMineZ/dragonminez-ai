# DMZ v2.2 quest objective types

Each objective is `{"type": "...", ...}` (* = optional):
- `ITEM`: `item`, `count`. Have them in the inventory; items are not taken.
- `KILL`: `entity` (id or `#tag`), `count`, `health`*, `meleeDamage`*, `kiDamage`*, `spawn`* (QUEST spawns it, NATURAL uses world mobs), `count_mode`* (QUEST_SPAWNED_ONLY or ANY_MATCHING), `AITier`*, `TextureVariant`*, `canTransform`* and `Transform*` stats.
- `SPAR`: like KILL without transforms; when the partner would drop below 15% HP the fight ends as a win for the party.
- `DELIVER`: `item`, `count`*, `npcId`; items are taken, partial deliveries count.
- `SURVIVE_WAVES`: `entity`, `waves`* (3), `mobs_per_wave`* (3), `wave_delay_seconds`* (10), stats.
- `ESCORT`: `entity`, `x`, `y`, `z`, `radius`* (6), `health`*; the quest fails if it dies.
- `CHECKPOINT_RACE`: `checkpoints` (list of x/y/z), `radius`* (8), in order.
- `BIOME`, `DIMENSION`, `STRUCTURE` (enter it), `COORDS` (`x`, `y`, `z`, `radius`* 10).
- `INTERACT`: `entity`* or `entityName`*. `TALK_TO`: `npcId`.
- `DRAGON_SUMMON`: `dragon`* and/or `ball_set`*.
- `SKILL`: `skill`, `level`* (1).

Without `parallel_objectives`, objectives go one at a time, except consecutive KILL objectives, which unlock together. A failed quest (time out, escort died) loses its progress and can be restarted.

Keywords: quest objective, objective types, KILL, SPAR, ITEM, DELIVER, SURVIVE_WAVES, ESCORT, CHECKPOINT_RACE, TALK_TO, DRAGON_SUMMON, STRUCTURE, COORDS, kill objective not counting, quest failed
