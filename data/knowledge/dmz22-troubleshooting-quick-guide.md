# DMZ v2.2 troubleshooting quick guide (common problems and where to look)

- Game will not start / missing dependency: Forge 47.4.10+, Java 17, GeckoLib, TerraBlender, Curios for 1.20.1. Remove Epic Fight, Better Combat, Legendary Tooltips.
- Cannot join server / version mismatch: client and server need the exact same DMZ jar (network fingerprint).
- Kicked "not allowed to play this Beta/Alpha version": beta whitelist; restart the server after being added.
- Values look wrong or "priceless" after updating: delete `config/dragonminez` (backup first) or check `config/dragonminez/oldBackup`.
- Chat shows "JSON load report": a JSON file has an error; the line names the file.
- Quests broken after an update: read `<world>/dragonminez/quest_update_report.txt`; last resort `/dmzrestoreupdate`.
- Cannot transform: form skill level (TP), saga quest requirement, 25 mastery on the previous form, or a legendary form (Movies saga only).
- Stuck dead in Otherworld: talk to Fortuneteller Baba, then King Yemma.
- Structure missing: `/dmzlocate` or `/place structure dragonminez:<id>`.
- Dragon balls lost: `/dmzdragonballs status` and `regenerate`.
- Died after reconnecting: combat logout kill (`killPlayersOnCombatLogout`).
- Progress not saving with a database: check the log for "Failed to connect to database".
- Low FPS: disable 3D auras, lower hair physics, disable impact frames.
- Bug reports: include versions, steps, `latest.log` and a `/dmzdebug` dump.

Keywords: troubleshooting, problem, not working, bug, crash, fix, help, broken, cant join, cant transform, lost progress, lag
