# DMZ v2.2 Human racial skills: Adrenaline, Fast Learner, Blood Fueled Ki, Android Core and Barrier

Human passives:
- Blood Fueled Ki: ki attacks cost 25% less Ki; part is paid with HP (12.5% of the cost), added as bonus damage on the next hit. Never below 2 HP.
- Fast Learner: techniques gain 35% more experience (`techniqueXpBonus`).
- Adrenaline: at 25% HP or below, for 15 seconds you take 30% less damage and get +25% attack and movement speed. Re-triggers after 180 seconds once HP rose back above the threshold.

Becoming an Android: accept Dr. Gero's conversion in his lab (Humans only, alignment 60 or lower, not while fused). It is permanent: Human Super and Legendary forms are replaced by the Android forms (starts in Android Base), Blood Fueled Ki is replaced by:
- Android Core: ki attacks cost 50% less Ki with no HP cost, Ki regen x2, but ki attacks deal 15% less damage.
- Absorption Barrier: select the racial skill in the X menu and hold G to raise a barrier up to 10 seconds. Ki blasts restore 50% of their power as Ki; overflowing max Ki by 50% triggers a Ki Surge, past 75% the barrier shatters and backfires. Cooldown 30 seconds (180 if shattered).
- An upgraded android's Battle Power is reported as the maximum value, so it cannot be measured normally.

Config: `racialSkills.human` in general-server.json (`adrenaline*`, `androidBarrier*`, `androidKiRegenMultiplier`, `techniqueXpBonus`).

Keywords: human racial, adrenaline, fast learner, blood fueled ki, android, android core, absorption barrier, dr gero, android upgrade, human passive
