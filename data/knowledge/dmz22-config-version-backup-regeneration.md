# DMZ v2.2 config updates, oldBackup folder and regenerated configs

Every DMZ config file has a `configVersion` field (2.2.0 for v2.2). On start or `/dmzreload`:

- Missing file: created with defaults.
- Outdated or missing version: the old file is moved to `config/dragonminez/oldBackup/<same path>`, a fresh default is written, and values you changed are copied back (a three-way merge against the old shipped defaults, so untouched values take the new default and your edits are kept). The log says "Regenerating X. Reason: ..." and "Preserved N user-modified value(s)".
- Malformed JSON (syntax error): DMZ does NOT overwrite your file. It runs on defaults for that session and reports the error. Fix the syntax and run `/dmzreload`.

So if values "reset" after an update, compare with `oldBackup/` (DMZ never reads it back; editing it does nothing). Custom races and custom form files keep their values. Unknown keys (typos) are ignored and listed in the JSON load report. Don't edit `configVersion` by hand. `//` comments and keys starting with `_` or `$` are tolerated but removed whenever DMZ rewrites the file. Files named `old_...` in forms, raids and tournaments folders are ignored.

"Priceless" form in the Skills menu = its level has no TP cost and no quest grants it, so a form or skill file is broken. Check the JSON load report first; last resort: stop the server, move `config/dragonminez` out as a backup, start again (configs only, not worlds or progress).

Keywords: configVersion, oldBackup, backup, regenerated, reset config, outdated config, malformed json, config wiped, values reset after update, priceless, delete config
