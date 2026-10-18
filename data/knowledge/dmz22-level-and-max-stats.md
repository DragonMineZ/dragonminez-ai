# DMZ v2.2 player level, max level and stat cap (maxValue)

Level is not XP-based. It is derived from your total stats: level = 1 + (total stats above your starting stats) / (max total stats above starting stats) x (maxValue - 1).

`gameplay.maxValue` in general-server.json (default 1,000,000) is both the max level and the stat cap:
- Max total stats = maxValue x 6.
- If `maxLevelValueInsteadOfStats` is true (default), only the total is capped, so one stat can go past maxValue.
- If false, each single stat is also capped at maxValue.

Lower maxValue for a shorter progression; level requirements of forms (`levelRequisite`) and quests use this level.

Attribute Points (AP) are spent before TP when raising stats, at no TP cost (granted by some rewards; `attributeRewardPointRatio` 0.5).

Max Power Release: 50% + 5% per Potential Unlock skill level, times racial bonuses.

Keywords: level, max level, maxValue, stat cap, total stats, level formula, maxLevelValueInsteadOfStats, attribute points, AP, power release, potential unlock
