# DMZ v2.2 transformations: how forms unlock, controls, mastery, drain

Unlocking the next form in a group needs all of: the form skill level (Super Forms, Legendary Forms or Android Forms, bought with TP in the Skills menu), 25% mastery on the previous form, and its story requisite (`sagaRequisite`, e.g. `frieza_saga:10`; locked forms say "Locked until: Frieza Saga - Quest 10"). Story requisites only apply with story mode on; toggles `sagaFormRequisiteEnabled`, `levelFormRequisiteEnabled`. Legendary lines (TP cost -1) come from Movies Saga quests 30, 34 and 40.

Controls: hold X and pick a form (Super Forms slice or Extra Forms), hold G to charge it. Double-tap G or the Instant Transform key transforms instantly (needs 40% mastery, costs 4x the drain in Ki). Alt+G descends one form.

Mastery (0-100% per form, only while active): 25% next form and stacking, 40% instant transform, 50% transform straight into it from base, 100% up to x1.5 stats and drain x0.75. Hits give 0.025% (only at 50%+ Power Release, more for big hits), plus 0.004% every 5 seconds. Mastery Capsules add 5%.

Drain: forms cost Ki (some stamina or health) scaled by Power Release, your raw offense versus max Ki, mastery and stacking. If you can't pay, you revert to base ("drained"). Multipliers of form, stack form and effects add up unless `multiplicationInsteadOfAdditionForMultipliers` is true.

Keywords: transformation, transform, form, unlock form, how to transform, cant transform, locked until, mastery, instant transform, descend, drained, form drain, saga requirement
