# DMZ v2.2 form JSON fields (editing or making forms)

A form file has `configVersion`, `groupName`, `formType` (superforms, legendaryforms, androidforms...) and `forms` (form name to form data). Useful fields per form:

- Unlock: `unlockOnSkillLevel`, `sagaRequisite` (`saga_id:questNumber`), `levelRequisite`, `formRequisite` (`group.form`), `formRequisiteType`, `unlockOnMastery`.
- Power: `strMultiplier`, `skpMultiplier`, `stmMultiplier`, `defMultiplier`, `vitMultiplier`, `pwrMultiplier`, `eneMultiplier`, `speedMultiplier`, `attackSpeed`.
- Cost: `energyDrain`, `staminaDrain`, `healthDrain`, `staminaDrainMultiplier`, `otherworldTimeDrain`, item costs (`triggerItemCosts`, `durationItemCosts`).
- Mastery: `maxMastery`, `masteryPerHitDealt`, `masteryPerHitReceived`, `passiveMasteryEveryFiveSeconds`, `maxStatsMultiplier`, `maxCostMultiplier`, `instantTransformOnMastery`, `stackOnMastery`, `shareMasteryWith`.
- Stacking: `formStackable`, `stackDrainMultiplier`, `incompatibleWith`.
- Looks: `customModel`, `modelScaling`, `hairType`, `hairColor`, `eye1Color`, `bodyColor1-3`, `auraType`, `auraColor`, `auraType3D`, `hasLightnings`, `outlineShader`, `transformationAnimation`, `mobEffects`.

`multiplicationInsteadOfAdditionForMultipliers` (general-server) changes how form and other multipliers combine. Run `/dmzreload` after edits.

Keywords: form json, form config, edit form, custom form, multiplier, strMultiplier, energyDrain, sagaRequisite, unlockOnSkillLevel, form stats, form drain, make my own form
