# DMZ v2.2 "JSON load report" chat message and JSON errors

When any DMZ JSON (configs, quests, wishes, dragon ball packs, dialogues) fails to parse or has unknown keys or types, DMZ records it. Each player who joins then sees "[DragonMineZ] JSON load report: N problem(s), M auto-update(s)" with up to 12 lines naming the file and the reason. The full list is in the server log.

- `[!]` lines are errors: the file or entry was skipped or defaults were used. Fix the JSON and run `/dmzreload`.
- `[~]` lines are automatic updates (for example quest files upgraded to new defaults).

Turn the chat message off with `developer.reportJsonProblemsInChat = false` in `general-server.json`. The problems are still logged.

Common causes: trailing commas, missing quotes, a misspelled `type` (quest objective, reward or wish type), or keys from an older DMZ version.

Keywords: json load report, json error, malformed json, problem, file skipped, reportJsonProblemsInChat, chat spam on join, invalid type
