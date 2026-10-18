# DMZ v2.2 classes and class passives (Warrior, Spiritualist, Martial Artist, Berserker, Paladin, Tank, Cleric, Unversed)

All races share 8 classes, tuned per race in `races/<race>/stats.json` under `classes` (baseStats, statScaling, regen, tpGainMultiplier, tpCostMultiplier, passive values):

- warrior (STR/RES/VIT): 3 clean melee hits in a row give a Fury stack (max 5), +10% STM regen each; at 5 stacks +10% defense penetration.
- spiritualist (PWR/ENE): damaging ki attacks get 20% less cooldown (debuffing ones 15% less, debuff lasts 25% longer).
- martialartist (SKP/VIT): strikes deal up to 25% more damage based on the target's missing HP (full bonus at 25% HP or lower).
- berserker (STR/VIT): below 66% HP +25% HP regen and +10% crit; below 33% +75% regen and +25% crit.
- paladin (SKP/RES/VIT): takes 15% of party members' damage (reduced by own defense, never below 1 HP) and heals for 15% of party damage dealt.
- tank (RES/VIT, 1.25x TP gain): 50% of stamina regen added as HP regen, healing received +25%, doubled below 30% HP.
- cleric (RES/ENE, 1.25x TP gain, 0.9x TP cost): healing ki attacks 20% less cooldown (buffing ones 15%, buff lasts 25% longer).
- unversed (balanced): ki attacks empower the next 3 melee hits or strike by 15%, and melee/strikes empower the next ki attack by 15%, 8s each.

Change class: `/dmzclass <class> [targets]` (dmzclass.set.self/others).

Keywords: class, classes, warrior, spiritualist, martial artist, berserker, paladin, tank, cleric, unversed, class passive, fury, resonance, change class, dmzclass
