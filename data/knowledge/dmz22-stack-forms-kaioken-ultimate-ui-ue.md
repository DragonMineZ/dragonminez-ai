# DMZ v2.2 stack forms: Kaioken and Ultimate

Stack forms are skills any race can learn. Files: `config/dragonminez/forms/` (kaioken.json, ultimate.json; ultrainstinct.json and ultraego.json also exist for later content).

- Kaioken: x2, x3, x4, x10, x20 (skill levels 1-5, bought with TP; `kaioken` costs in skills.json). Taught by King Kai. Drains health, not Ki. Each tier passes 25% of its mastery gains to the next. Turns off on death.
- Ultimate: not bought with TP (cost -1). Clear Old Kai's Ultimate Challenge on the Sacred World of the Kai with alignment above 61 and Potential Unlock level 10. Ultimate is not stacked on top of another form: it copies your best unlocked form and adds its own bonus (never your race's top tier). `ultimateFormFixedValue` (general-server) makes it a fixed multiplier instead.

Stacking rules: your current form needs 25% mastery (`stackOnMastery`), otherwise the stack form is dropped when you transform. Stacking multiplies the drain of both forms (`stackDrainMultiplier` 2.0). Descend (Alt+G) removes the stack form first. `incompatibleWith` blocks combinations.

Admin: `/dmzform` with the stack group, `/dmzmastery ... stack ...`, `/dmztrigger <player> stackform <group> <form>`.

Keywords: kaioken, x2, x10, x20, ultimate, mystic, old kai, stack form, stacking, cant stack, stackOnMastery, kaioken drains health
