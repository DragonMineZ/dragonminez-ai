# DMZ v2.2 addons and modding with DragonMineZ (what support covers)

Most DMZ content is data, so server owners and pack makers can customize it without code:
- Configs: races (including custom races), forms, stack forms, skills, techniques, combat, raids, tournaments.
- World data: sagas, quests, sidequests, wishes, dialogues, NPC placements.
- Dragon ball packs (folder or zip in `<game dir>/dragonballs/`).
- Datapacks: space pod destinations (`data/<ns>/spacepod/`), structure NPCs.

For Forge add-on developers, DMZ exposes events (`DMZEvent`: TP gain, form changes, regen and more), registries for new quest objective and reward types (QuestObjectiveRegistry, QuestRewardRegistry) and utility menu extensions; the wiki's Add-on Development, Implementation and Events and Creating a DMZ Addon pages cover them. There is no Java API to register new races, forms, skills or techniques: those are JSON only. The mod is open source (GPL-3.0) at github.com/DragonMineZ/dragonminez.

Support scope: explain how DMZ systems work, which config keys, JSON fields, events or registries apply, and why something behaves the way it does. Support does not write addon code, mixins, Java classes or full datapacks for people; point them to the relevant file, field or API and the source code instead.

Keywords: addon, addons, modding, mod developer, api, events, DMZEvent, custom content, datapack, make an addon, source code, github, java, how to code
