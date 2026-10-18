# DMZ v2.2 techniques: ki attacks, strike attacks, evasions (techniques.json)

Techniques go in 8 slots (Alt+1-4, Ctrl+1-4): ki attacks (beams, blasts, disks, barrages), strike attacks and evasions. Unlocked from masters, quests and world bosses; they gain experience by use.

`config/dragonminez/techniques.json` has one entry per technique in `kiAttacks`, `strikeAttacks` or `evasionAttacks`:
- `damageMultiplier`, `kiCostMultiplier`, `destructionMultiplier` (block damage), `maxDestructionRadius`.
- `castTimeTicks`, `cooldownTicks` (strikes and evasions; strikes default 80).
- Experience: `minXPCost`, `maxXPCost`, `xpCostMultiplier`, `xpGainMultiplier`, `xpGainPerHit`, `xpGainPerKill`.
- Size: `baseSizeCap`, `transformedSizeCap`, `sizePowerFloor`, `sizePowerCeiling`.
Missing entries are re-added automatically.

Firing any technique needs the Ki Control skill, at least 5% Power Release and an empty main hand. Ki attacks charge while you hold the key; strikes and evasions fire on press. Every strike starts a shared 3 second cooldown (halved on a miss; `strikeGlobalCooldownTicks` 60); with no target in reach a strike rushes forward and a full miss refunds part of its Ki. Players can restyle ki techniques (colors, shape).

Admin: `/dmztech add|remove <technique> [targets]`, `/dmztech experience <amount>`, `/dmzcooldowns <slot> <cooldown> [targets]`.

Keywords: techniques, technique, ki attack, kamehameha, beam, strike attack, evasion, technique slot, hotbar, alt 1, ctrl 1, techniques.json, damage multiplier, cooldown, technique xp, dmztech
