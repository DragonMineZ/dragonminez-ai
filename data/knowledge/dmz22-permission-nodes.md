# DMZ v2.2 permission nodes (LuckPerms and other permission mods)

DMZ registers Forge permission nodes under `dragonminez.`. A player can use a command if they have its node, or `dragonminez.admin`, or op level 2. The console can always use them.

Allowed for everyone by default: `dmzstats.info.self`, `dmzskill.list.self`, `dmztech.list.self`, `dmzeffect.list.self`, `dmzpoints.info.self`, `dmzquest.list.self`, `dmzquest.info`, `dmzquest.track.self`, `dmzparty.use`, `dmzform.list.self`, `dmzalignment.info.self`, `dmztail.self`, `dmzhair.self`.

Everything else defaults to false (ops only). Main nodes, each with `.self` and `.others` variants:
- `dmzstats.set|add|info|reset`, `dmzpoints.set|add|remove|info`, `dmzbonus.add|clear`
- `dmzskill.set|add|remove|list`, `dmztech.add|remove|list|experience`, `dmzcooldowns`
- `dmzeffect.give|remove|clear|list`, `dmzform.set|add|remove|list`, `dmzracial.reset`
- `dmzquest.start|finish|fail|reset|track|startsaga|finishsaga|resetsaga|difficulty`
- `dmzalignment.set|add|remove|info`, `dmzrevive`, `dmzhalo`, `dmzhair`, `dmztail`, `dmzclass.set`, `dmzminigame`, `dmztrigger`
- Single nodes: `admin`, `dmz.reload`, `dmzlocate`, `dmzstructure`, `dmzdragonballs.status`, `dmzdragonballs.regenerate`, `dmzmastery.set`, `dmzmastery.add`, `dmzweight.give`.

Example (LuckPerms): `/lp group mod permission set dragonminez.dmzrevive.others true`. Raid, world boss and tournament commands need op level 2.

Keywords: permissions, permission nodes, luckperms, dragonminez.admin, op, cant use command, no permission, command permission, self, others
