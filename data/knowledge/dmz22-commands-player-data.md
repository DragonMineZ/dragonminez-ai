# DMZ v2.2 admin commands for player data (stats, points, forms, skills)

`[targets]` is optional; without it the command affects yourself.
- `/dmzstats set|add|remove <STR|SKP|RES|VIT|PWR|ENE|ALL> <amount|min|max> [targets]`, `/dmzstats reset [targets] [keepPercentage] [keepSkills]`, `/dmzstats relocate [targets]` (refund stats as points to reassign).
- `/dmzpoints set|add|remove <amount> [targets]`: TP.
- `/dmzbonus add <stat> <+|-|x|*> <value> <bonusName> <applyMultipliers> [targets]`, `/dmzbonus remove|clear`: bonus stats (STR, SKP, DEF, STM, VIT, PWR, ENE).
- `/dmzform set|add|remove ...`, `/dmzform set all <level>`: forms.
- `/dmzmastery ...`: form and stack form mastery.
- `/dmzskill set|add|remove <skill> <level> [targets]`.
- `/dmztech add|remove <technique> [targets]`, `/dmztech experience <amount>`.
- `/dmzcooldowns <slot> <cooldown> [targets]`: technique cooldowns.
- `/dmzeffect give|remove|clear|list`: DMZ effects.
- `/dmzclass <class> [targets]`, `/dmzalignment set|add|remove <amount> [targets]`.
- `/dmzracial reset [targets]`: racial progress (Zenkai, assimilations, absorptions, Cell Jrs).
- `/dmzrevive [targets]`, `/dmzhalo on|off`, `/dmztail cut|grow`, `/dmzhair resync|reset`.
- `/dmztrigger <player> form|stackform|racialskill|rage|knockout|release ...`: test forms and racial skills.

Permissions: each has `.self` and `.others` nodes (see the permissions doc). Op level 2 has all of them.

Keywords: commands, admin commands, dmzstats, dmzpoints, dmzbonus, dmzform, dmzmastery, dmzskill, dmztech, dmzcooldowns, dmzeffect, dmzclass, dmzalignment, dmzracial, dmzrevive, dmztrigger, give tp, set stats
