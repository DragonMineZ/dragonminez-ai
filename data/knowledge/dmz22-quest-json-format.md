# DMZ v2.2 quest JSON format (custom quests and sagas)

Saga manifest `<world>/dragonminez/sagas/<id>.json`: `id`, `name` (translation key), `requirements.previousSaga`, `questFolder`. Its quests are the JSON files in `quests/<questFolder>/`, ordered by filename: filename order is the play order (`01_...json`, `02_...json`; insert with `01b_...`). A saga quest is `<sagaId>:<numeric id>`, not the filename; duplicate ids in a folder overwrite each other. Sidequests go anywhere under `sidequests/`. Missing default files come back while the `createDefault...` toggles are on; your own filenames are never touched.

Quest file fields:
- Required: `id`, `title` (translation key or text), `type` (SAGA, SIDEQUEST, DAILY = once per real day, EVENT).
- Optional: `description`, `category`, `quest_giver`, `turn_in`, `claim_mode` (TREE_OR_NPC or NPC_ONLY), `parallel_objectives`, `party_scaling`, `secret` (hidden until available), `repeatable`, `repeat_cooldown_seconds`, `time_limit_seconds` (in-game seconds, then the quest fails).
- `prerequisites`: conditions to unlock the quest; `requirements`: conditions to start it (for example being in a biome). Both use the same syntax (see the prerequisites doc).
- `objectives` and `rewards`: lists of objects with a `type` (see those docs).

Unknown `type` values or keys are reported in the JSON load report and skipped. Run `/dmzreload story` after edits. Add-on mods can register new objective and reward types.

Keywords: quest json, custom quest, make a quest, quest format, saga json, questFolder, sidequest json, quest fields, claim_mode, repeatable quest, daily quest
