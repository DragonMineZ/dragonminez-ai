# DMZ v2.2 parties (/dmzparty)

Commands (permission dmzparty.use, on by default):
- `/dmzparty invite <player>`, `accept`, `confirm <party>`, `reject`, `leave`, `list`, `kick <player>`, `disband`, `pvp` (toggle friendly fire).
The party tab is also in the menu (V).

Rules (general-server.json, gameplay):
- `partyMaxMembers` (-1 = unlimited), `partyMaxLevelGap` 500.
- `partyTpShareRatio` 0.5: members get 50% of the TP you earn, but only if you know Instant Transmission and they are in the same dimension within its range (200 blocks per level).
- Quests with `party_scaling` scale enemies: `enemyHealthPerPartyPlayer` 1.25 and `enemyDamagePerPartyPlayer` 1.1 per extra player; `defaultQuestPartyMultiplier` 1.45.
- `partyMarkerRange` 2000: party member markers on screen (`partyMarkers` in general-user.json hides them).
- Quests: everyone keeps their own progress; members within 128 blocks who reached that point join when you start one; kills count for nearby members on it; if all of them die the quest fails. The leader's difficulty applies.
- The Paladin passive only works on party members.

Party data is saved per world.

Keywords: party, parties, dmzparty, invite, group, team, party quest, share tp, party pvp, friendly fire, party markers, party size
