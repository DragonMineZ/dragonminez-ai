# DMZ v2.2 Training Points (TP): how to earn TP and what stats cost

TP buys stats, skills, techniques and form levels. Default sources (general-server.json `gameplay`):
- Hits: 2 TP per hit of 1+ damage (`tpPerHit`). Kills: per-hit TP plus 25% of the target's max health.
- Passive 1 TP every 5 seconds, 1 per 20 blocks traveled, 1 per block mined, 1 per item crafted (times rarity/tier).
- Training minigames (biggest deliberate source), Shadow Dummy (+25% to +75%).
- Quests pay Attributes: half as Attribute Points, half as the TP those points cost now. Raids and tournaments pay TP lump sums; world bosses pay Attributes.
- Party: members get 50% of your TP if you know Instant Transmission and they are within its range. Fusion: partner gets half.

Boosts add up (each adds on top of x1.0): class (Tank, Cleric 1.25), Frost Demon 1.25, Time Chamber 1.75, dead in Otherworld 2.0, gravity and weights, TP Multiplier effect (+25%/level), Hero of the World (+25% 10 minutes after a raid win), Kami's Blessing (1.2 for 5 minutes, from Dende for Good players), Mutant 1.25, difficulty 1.25 on lump sums, and progression up to +75% toward level 10,000 (`increaseTPGainRelativeToTPCost`). Global `tpGainMultiplier`; per-source rules in `tpGainBoosts`.

Stat cost: `minTPCost` (16) plus an amount growing with total stats, times `globalTPCostMultiplier` and race/class multipliers, minus an early discount below `maxTPDiscount` (140) total stats.

`/dmzpoints set|add|remove <amount> [targets]`.

Keywords: TP, training points, TP gain, TP cost, how to get tp, tp boost, tpGainMultiplier, minTPCost, party tp share, kami blessing, TP too slow, dmzpoints
