# DMZ v2.2 quest files after an update: auto-upgrade, report, /dmzrestoreupdate

Quest, saga and sidequest files live in the world (`<world>/dragonminez/`) and carry a `defaultsVersion` (2.2.0). With `autoUpdateQuests` true (general-server gameplay, default), on server start DMZ upgrades old files in place:
- Values you never changed take the new default.
- Values you edited are kept (listed as CONFLICT).
- Fields you deleted stay deleted.
- Obsolete default files from older versions are retired.
Originals go to `<world>/dragonminez/oldBackup/` and a summary is written to `<world>/dragonminez/quest_update_report.txt`. The JSON load report in chat also mentions it.

Quests stuck, duplicated or broken after updating (for example Merchant or Shin quests): run `/dmzrestoreupdate`, then click CONFIRM or run `/dmzrestoreupdate confirm` within 15 seconds (admin). It DELETES everything in the world's `wishes`, `sagas` and `quests` folders, regenerates the defaults and reloads. Custom quests and wishes in those folders are lost, so back them up first. Player progress is stored with the player, not in those folders.

To stop automatic changes to custom quest packs, set `autoUpdateQuests` false.

Keywords: quest update, quests broken after update, quest_update_report, defaultsVersion, autoUpdateQuests, dmzrestoreupdate, restore quests, reset quests files, merchant quest bugged, shin quest
