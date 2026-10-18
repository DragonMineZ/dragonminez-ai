# DMZ v2.2 blocking, parrying, guard break, passive dodge, perfect evasion

Block: hold right-click with empty hands. Reduces hit damage by 5% to 65% (scaled by defense); each blocked hit costs stamina equal to a quarter of the damage blocked. You cannot deal melee damage while blocking.

Poise and guard break: part of blocked damage also hits your Poise (25 + a defense-based amount). When poise or stamina runs out your guard breaks: stunned 3 seconds and hits ignore 66% of your defense. Poise regenerates 5 seconds after its last damage.

Parry: start blocking within 150 ms of a hit. You take no damage, the attacker is knocked back and staggered 3 seconds (non-players also stunned), ki blasts are deflected and strikes countered, and the attacker's melee costs double stamina for 4 seconds.

Passive dodge: needs the Meditation skill. Against players you must be faster (2.5x their Speed at Meditation level 1, 1.4x at level 10); against other enemies your Battle Power must be higher (2x at level 1, 1x at level 10). Chance starts at 10% and goes up to 35% vs players and 50% vs others at level 10 (60% of that at level 1). Each dodge costs 3% max stamina, at least 0.5 seconds apart, and consecutive dodges get less likely (x0.75) until 3 seconds without one.

Perfect evasion: dash (R) within 200 ms of an attack; 1.5 second lockout after one.

Settings in combat.json (`enableBlocking`, `enableParrying`, `parryWindowMs`, `enableSpeedDodge`, `speedDodge*`, `perfectEvasionWindowMs`).

Keywords: block, blocking, parry, parrying, poise, guard break, stun, dodge, passive dodge, meditation, evasion, perfect evasion, speed, cant block, auto dodge
