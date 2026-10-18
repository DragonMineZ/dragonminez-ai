# DMZ v2.2 damage and defense mitigation (how hits are reduced)

Defense = RES x class DEF scaling + armor (armor x 0.5 + toughness x 0.7) + DEF bonuses, multiplied by Power Release and the form's DEF multiplier. Fighting uncharged makes you much squishier.

A hit goes through these stages (combat.json defaults):
1. Penetration: attacker's defense penetration (Defense Penetration skill and enchantment 2.5% per level, ki technique penetration, Warrior passive), capped at 50%. Guard broken: defense cut 66%.
2. Negation: if flat defense is at least 2.5x the damage, the hit is negated (`cancelDamageMitigationThreshold`).
3. Flat mitigation: flat defense absorbs up to 82% of the hit (`flatMitigationMaxAbsorbFraction`).
4. Percent reduction with diminishing returns, capped at 88% (`baseDamageReductionCap`).
5. Protection enchantments, combined cap 93% (`enchantmentDamageReductionCap`).
6. Adaptive mitigation: a hit equal to your flat defense loses another 35%, fading to nothing at 14x, max 65% (`adaptativeMitigation*`).
7. Blocking (5% to 65%) and Ki Protection.

Knockback scales with damage taken: a mostly absorbed hit pushes 15%; a hit of 15%+ of max HP is heavy, up to 2.5x.

Melee windup is 3 to 7 ticks, checked server-side against the real hitbox; at least 2 ticks between attacks; giant enemies take one hit per swing.

"Unkillable" or "one-shot" balance: look at these caps, `vitCurveKnee`/`defCurveKnee` in general-server, form multipliers and Power Release.

Keywords: damage, defense, mitigation, damage reduction, unkillable, one shot, tanky, armor, penetration, negated, combat.json, knockback
