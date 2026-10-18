# DMZ v2.2 quest reward types and prerequisites

Rewards (`rewards` list, each with `type`; any reward can be limited with `difficulty`/`difficulties`/`minDifficulty`):
- `ITEM`: `item`, `count`. `GENERIC_ITEM`: item with `enchantments`, `potion`, `mobEffects` and similar.
- `TPS`: `amount` TP. `ATTRIBUTES`: `amount` (half Attribute Points, half the TP they cost; what default quests use). `ATTRIBUTE_POINTS`: `amount` AP.
- `ALIGNMENT`: `amount` (+/-).
- `COMMAND`: `command` run at operator level, `%player%` replaced; optional `translationKey` shown instead.
- `SKILL`: `skill`, `level`.
- `TRANSFORMATION`: `formGroup`, `formName`, `mastery` (default 100), `stack`.
- `KI_TECHNIQUE`: `code` (exported technique code).
Rewards are per player: each party member claims their own.

Conditions (quest `prerequisites` to unlock and `requirements` to start; each block is `operator` AND/OR plus a `conditions` list, each with `type`; groups can nest):
- `SAGA_QUEST` (`sagaId`, `questId` number), `QUEST` (`questId`)
- `LEVEL` (`minLevel`), `STAT` (`stat`, `minValue`)
- `ALIGNMENT` (`min`, `max`), `RACE` (`race`: human, saiyan, namekian, frostdemon, bioandroid, majin, glind), `CLASS` (`class`), `SKILL` (`skill`, `minLevel`)
- `BIOME`, `DIMENSION`, `STRUCTURE` (`structure`, optional `hint` with dimension/x/y/z), `TIME` (`mode` GAME_TIME...)
Dialogue choices use the same condition syntax. A malformed condition makes the choice disappear instead of opening unconditionally.

Keywords: quest rewards, reward types, TPS reward, command reward, transformation reward, prerequisites, quest requirements, quest locked, conditions, LEVEL, ALIGNMENT, SAGA_QUEST
