# DMZ v2.2 story mode: sagas, order, difficulty

Nine default sagas; each opens once every quest of the previous one is done:
1. classic_saga (52 quests) 2. saiyan_saga (16) 3. frieza_saga (22) 4. android_saga (22)
5. future_saga (7) and buu_saga (42), both after Android, any order
6. gt_saga (46) and daima_saga (17), both after Buu, any order
movies_saga (40) runs alongside: its first quest opens after Defeat Raditz (Saiyan Saga #1), later movies follow main story progress. Movies quests 30, 34 and 40 give the Legendary forms.
Form requirements use `saga_id:number`, e.g. `frieza_saga:10`. Many quests must be started in a specific biome, structure or dimension.

Difficulty (per player): picked the first time you open the Quest Tree, permanent. Easy: enemies x0.75 HP, x0.5 damage, x1.25 TP, Novice AI. Normal: Smart AI. Hard: x2 HP, x1.5 damage, x1.25 TP, x1.25 quest rewards, Elite AI. Change it only with the Change Difficulty wish (keeps progress). In a party the leader's difficulty applies; joining a higher one asks for confirmation and permanently raises yours. Admins: `/dmzquest difficulty <difficulty> [targets]`. Values: `easyMode*`, `hardMode*`.

Settings (gameplay): `storyModeEnabled`, `createDefaultSagas`, `sideQuestsEnabled`, `createDefaultSideQuests`, `autoUpdateQuests`. The Reset Story wish lets you replay, but quest rewards are x`storyResetTPMultiplier` (0.5) per reset.

Keywords: story, story mode, saga, sagas, saga order, classic, saiyan saga, frieza saga, android saga, buu saga, gt saga, daima saga, movies saga, difficulty, easy, hard, quest tree, party quest
