# DMZ v2.2 wishes: default wishes and editing wish files

Wish lists are per world: `<world>/dragonminez/wishes/<dragon>.json` (shenron.json, porunga.json; the file name is the dragon id). Each file is a JSON array of wishes. Missing default files are recreated on start; reload with `/dmzreload wishes`.

Default Shenron wishes: senzu beans, attributes, Power Pole, Might Tree fruit, Namek radar CPU, Saiyan Spaceship, racial skill reset, re-customize character, change difficulty, reset story, revive, armor materials (Kikono shards and iron), The Strongest armor.
Default Porunga wishes: senzu, attributes, Brave Sword, racial reset, re-customize, relocate stats, change difficulty, reset story, revive, armor materials, Invincible armor (classic and blue), yellow and green Potara earrings.

Every wish has `name`, `description` (translation keys, convention `wish.<dragon>.<key>.name/.desc`), `type`, optional `repeatable` (default true; false = once per summon on multi-wish dragons). Types and fields:
- `item` (`itemId`, `count`), `multi_wish` (`items`: list of `{"a": id, "b": count}`), `item_list_wish` (`items`: typed item objects)
- `command` (`commands` string list, `%player%` replaced)
- `tps` (`amount`), `attributes` (`amount`), `attributepoints` (`amount`)
- `skill` (`skill`, `level`)
- `revive` (`maxTargets`), `passivereset`, `recustomize`, `relocatestats`, `changedifficulty`, `resetstory`

Add-on packs can add wishes too; world files win.

Keywords: wishes, wish, shenron wishes, porunga wishes, custom wish, add wish, wish json, wish types, command wish, revive wish, wishes not showing
