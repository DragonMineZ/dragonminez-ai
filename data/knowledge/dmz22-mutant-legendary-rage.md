# DMZ v2.2 Mutant status, Rage meter and legendary forms

Mutant is a random server-wide roll, not something you buy. Every 30 minutes the server rolls (20% chance) to make 1 online player a Mutant, with at most 1 Mutant at a time by default. A Mutant gets 1.25x TP, 1.5x form mastery gain, a Rage meter, and a stronger legendary line (`powerBonusBoostWithSkill`). The status is lost on death unless `keepMutantOnDeath` is true.

Rage (Mutants and False Super Saiyan): the meter fills from damage dealt and taken (more for big hits and for varied attacks), stays full for 10 seconds while active, and drains when idle.

Legendary forms (`legendaryforms` group of every race, 3 levels) are not bought with TP: Movies Saga quests 30, 34 and 40 grant Legendary Forms levels 1 to 3. With a full rage meter a Mutant can transform into Legendary forms they don't own yet: a borrowed form is worth your best unlocked non-legendary form +0.5 to each stat bonus (`rageBorrowedFormBonus`), capped at the real form. A full meter inside a Legendary form gives 10 seconds of active rage, +33% to owned Legendary forms. Mutants never get False Super Saiyan. Admins grant the status with `/dmzeffect give mutant -1 <player>`.

Config in general-server.json, `mutant` section: `enabled`, `rollIntervalMinutes`, `playersPerRoll`, `chance`, `maxHolders`, `tpGainMultiplier`, `masteryGainMultiplier`, `keepMutantOnDeath`, and `rage` (gain and drain values).

Test: `/dmztrigger <player> rage <value>`.

Keywords: mutant, mutation, legendary, legendary forms, rage, rage meter, mutant chance, become mutant, lost mutant on death, keepMutantOnDeath
