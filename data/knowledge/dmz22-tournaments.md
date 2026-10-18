# DMZ v2.2 tournaments (Baba's Tournament, Cell Games, Otherworld Tournament)

- `baba`: Fortuneteller Baba's Tournament at her palace, one-star gauntlet ending with Kid Goku.
- `cell`: The Cell Games at the Cell Arena, lethal bracket ending with Super Perfect Cell. Cell only talks to Evil players (alignment 40 or less).
- `otherworld`: The Otherworld Tournament (talk to the Grand Kai, press Tournament), five-star bracket ending with Gogeta.

Entering: talk to the host within 12 blocks, Tournament, Enter. In a party only the leader enters and the whole party joins; everyone has 60 seconds to accept the rules. One run per tournament at a time.
Bouts: 30 seconds to press Ready, then a 3-2-1 countdown. Only your opponent can hurt you. Time limits (Baba 5, Cell 7, Otherworld 8 minutes); a timed-out bout is replayed. Stay within 60 blocks of the ring or you have 15 seconds to return. Losing ends your run and starts the re-entry cooldown. Lethal tournaments (Cell) can really kill you; knockout ones can't. Qualifiers get 8% stronger per round (Baba 10%).

Config: `config/dragonminez/tournaments/<id>.json` (`format` BRACKET or GAUNTLET, cooldowns, timers, `lethal`, `ring`, fighters, `rewards`). Extra files become new tournaments.

Admin (op): `/dmztournament ring <tournament>` saves the ring position where you stand; `/dmztournament cooldown clear <players> [tournament]`.

Keywords: tournament, tournaments, baba tournament, cell games, otherworld tournament, gogeta, bracket, gauntlet, ready, tournament cooldown, cant enter tournament, dmztournament
