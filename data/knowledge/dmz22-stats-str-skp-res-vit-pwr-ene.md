# DMZ v2.2 stats explained: STR, SKP, RES, VIT, PWR, ENE, Battle Power, Speed

- STR: melee damage (melee stamina cost grows with damage). SKP: strike technique damage. PWR: ki damage (Ki cost grows with damage).
- RES: max Stamina (20 + RES x stamina scaling), defense and Speed.
- VIT: max HP (vanilla 20 + VIT bonus) and HP regen; helps stamina regen.
- ENE: max Ki (20 + ENE x scaling).
DEF and STM appear in scalings and bonuses (both come from RES).

VIT and flat DEF grow on a curve: each point is worth more as you invest, ramping up to 4x the class rate at the knee (16,000 VIT, 20,000 RES; `vitCurveKnee`, `defCurveKnee`), then flat. Defense is multiplied by Power Release.

Speed = 1 + RES/10,000 x form speed x Power Release (x2 on the RES part while transformed), movement capped at x3, +0.5 step-up at Speed 2.

Crits: base 5% chance and x1.5 damage; Critical Chance/Damage enchantments add 5% per level.

Battle Power = 1,200 x (raw power / 100)^1.2 x Power Release, where raw power is STR + SKP + RES + PWR (with scalings, forms, bonuses) plus VIT and ENE at half weight. Upgraded Androids report max BP. At 0% release you are hidden from Ki Sense.

Admin: `/dmzstats set|add|remove <STR|SKP|RES|VIT|PWR|ENE|ALL> <amount|min|max> [targets]`, `/dmzstats reset [targets] [keepPercentage] [keepSkills]`, `/dmzstats relocate` (stats back as Attribute Points).

Keywords: stats, STR, SKP, RES, VIT, PWR, ENE, DEF, STM, strength, resistance, vitality, energy, battle power, BP, speed, crit, health, ki, stamina, dmzstats, relocate stats
