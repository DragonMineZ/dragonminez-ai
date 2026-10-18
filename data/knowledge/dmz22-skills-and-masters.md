# DMZ v2.2 skills and which master teaches them (skills.json)

Skills are learned from master NPCs, then leveled with TP in the Skills menu (V). Costs per level and allowed races are in `config/dragonminez/skills.json` (`skills.<id>.costs`, `allowedRaces`). A cost of -1 means that level cannot be bought.

Utility skills: fly, jump, sprint, meditation, kisense, kicontrol, kiboost, kimanipulation, kiprotection, ki_infusion, potentialunlock, defense_penetration, healing_reduction, instant_transmission, fusion, kaioken.

Masters (`skillOfferings` in skills.json):
- Roshi: jump, meditation, kicontrol, kamehameha, afterimage.
- Goku: fly, instant transmission, fusion, kamehameha, spirit bomb, dragon fist...
- King Kai: kaioken, potential unlock, ki manipulation, kaioken attack, spirit bomb.
- Vegeta: defense penetration, potential unlock, galick gun, big bang, final flash, fake moon...
- Gohan: ki boost, ki protection, ki sense, masenko, makankosappo.
- Piccolo: potential unlock, masenko, makankosappo, hellzone grenade.
- Krillin: sprint, ki sense, destructo disc, solar flare (taiyoken), afterimage.
- Old Kai: ki infusion, healing reduction, soul punisher (and grants Ultimate).
- Also Frieza, Trunks, Cell, Yamcha, Baba, Buu (each with their own list). Good masters (Goku, Gohan, King Kai, Old Kai, Roshi, Krillin) need alignment above 60; evil masters (Cell, Frieza) need 40 or less.

Admin: `/dmzskill set|add|remove <skill> <level> [targets]`.

Keywords: skills, learn skill, master, masters, teacher, who teaches, roshi, goku, king kai, vegeta, piccolo, krillin, old kai, skills.json, fly skill, ki sense, potential unlock, dmzskill
