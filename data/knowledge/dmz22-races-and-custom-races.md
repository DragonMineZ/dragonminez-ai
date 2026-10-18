# DMZ v2.2 races (Human, Saiyan, Namekian, Frost Demon, Bio-Android, Majin, Glind) and custom races

Playable races in v2.2, with their config folder names: human, saiyan, namekian, frostdemon, bioandroid, majin, glind. Human, Saiyan, Majin and Glind can pick a gender; the others cannot. Saiyans have a tail (`/dmztail cut|grow`).

Each race has `config/dragonminez/races/<race>/`:
- `character.json`: model, default colors, body/hair/eye defaults, head bones, racial skill id, and TP costs of each form group (`formSkillTpCosts`). A cost of -1 means the level cannot be bought with TP (unlocked another way).
- `stats.json`: per class base stats, stat scaling, HP/Ki/Stamina regen and class passive values.
- `forms/*.json`: one file per form group.

Custom races: create a new folder under `races/` (for example `races/android21/`) and restart or `/dmzreload`. DMZ fills in default character.json and stats.json for it and loads any form files you add. It shows up in character creation after the defaults. Custom races start with no form TP costs and use the human racial skill unless you change `racialSkill`.

`forceCharacterCreation` (general-server, gameplay) makes new players open character creation before playing.

Keywords: race, races, human, saiyan, namekian, frost demon, frostdemon, bio android, bioandroid, majin, glind, custom race, add race, character.json, stats.json, gender, tail
