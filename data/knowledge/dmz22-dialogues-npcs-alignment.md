# DMZ v2.2 custom NPC dialogues and NPC placements

Dialogues: `<world>/dragonminez/dialogues/*.json`. A tree has `npc` (master id; an id starting with `_` keeps the file inactive, which is how `_example.json` stays off), `start` (default "start") and `nodes`. Each node has `line` (translation key or text) and optional `choices`. Each choice has `text`, `goto` (leave out to end), optional `conditions` (quest prerequisite syntax; an unreadable block drops the choice) and `actions`:
- ALIGNMENT (`amount`, negative lowers), TPS (`amount`, positive only)
- COMMAND (`command`, run at operator level, `%player%` replaced)
- START_QUEST (`quest`: side quest id or `sagaId:questId`, normal start rules)
- OPEN_QUESTS (switches to the NPC's quest list and ends the tree)
Reload with `/dmzreload story`.

NPC placements: `<world>/dragonminez/npcs/placements.json` (`placements` list): id, entity, dimension, optional npcId, model, texture, structure (goku_house, roshi_house, elder_guru, timechamber, kamilookout, gero_lab), coordinates or `surface`, `relativeToSpawn`, `enabled`, `override`, alignment. Interaction rules by alignment: `npcs/alignment_rules.json`. Reload with `/dmzreload config` (respawns configured NPCs).

Testing: `/dmzquest questnpc spawn <npcId> <model> <texture>`, `/dmzstructure npcs`.

Keywords: dialogue, dialogues, custom dialogue, npc, npcs, npc placement, placements.json, alignment_rules, npc not spawning, dialogue not showing, _example.json
