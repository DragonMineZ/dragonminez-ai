# DMZ v2.2 False Super Saiyan (Saiyan rage form)

False Super Saiyan (`falseform` group, form `falsesupersaiyan`) is a hidden Saiyan form triggered by rage; it can't be bought with TP or selected.

Who can get it: Saiyans who have not bought any Super Forms skill level, are not Mutants, and are level 100 to 199 (`minLevel`, `maxLevel`).

How: while untransformed and at 25% HP or lower (`healthThreshold`), a fury meter fills from significant damage dealt and taken (and slowly while idle). When full you transform ("Your fury erupts... False Super Saiyan!"). It ends when the rage drains after combat (about 8 seconds, 12 while active) with "Your fury burns out...".

You cannot descend out of it, transform into another form from it, or stack a form on it.

Config: `falseSuperSaiyan` in general-server.json (`enabled`, `groupName`, `formName`, `minLevel`, `maxLevel`, `healthThreshold`, `requireZenkaiSpent`, gain and drain values). Test: `/dmztrigger <player> rage <value>`.

Keywords: false super saiyan, false ssj, fssj, rage form, saiyan rage, fury, cant get false super saiyan, hidden form
