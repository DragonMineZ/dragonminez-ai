# DMZ v2.2 dynamic growth (stats grow by practicing them)

With `dynamicGrowth.enabled` (default true in general-server.json), stats also grow by using them, on top of buying them with TP:
- Melee damage trains STR, strike techniques SKP, ki attacks and ki weapons PWR, damage taken VIT, stamina spent RES, Ki spent ENE. Linked pairs (STR-SKP, RES-VIT, PWR-ENE) get 50% of each other's practice (`linkedStatXpShare`). XP per point = 70 + 2S + S^1.35 (S = current stat; `practiceCurveEnabled`). Players can toggle growth per stat in the menu's Configuration screen (Stat Growth).
- Per-stat speed: `strPracticeMultiplier`, `skpPracticeMultiplier`, `resPracticeMultiplier` (1.5), `vitPracticeMultiplier` (0.5), `pwrPracticeMultiplier`, `enePracticeMultiplier` (1.5); global `practiceXpMultiplier` 2.0.

Anti-farming: practice against passive animals (0.5x), villagers (0.1x), training dummies (0.35x), low-damage hits (0.2x) and no-risk fights (0.5x) is reduced. Hitting the same target repeatedly within 30 seconds drops to 0.5x after 8 hits and 0.15x after 24.

`growthStatsCountForTpCost` false (default): stats gained by practice do not make TP purchases more expensive. `manualTpPurchasesEnabled` false makes stats grow only through practice. `attributeTpCostMultiplier` scales TP prices.

Keywords: dynamic growth, practice, stats grow by using, training by fighting, practice xp, farm, dummy, growth stats, manualTpPurchasesEnabled
