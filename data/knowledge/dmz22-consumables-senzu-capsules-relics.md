# DMZ v2.2 senzu beans, capsules and food

- Senzu Bean: fully restores HP, Ki and stamina (eat cooldown `senzuCooldownTicks` 240). Health, Ki and Stamina beans restore one resource each and can be grown from seeds; one of each plus Namek Moss crafts 3 regular beans. Sources: Korin gives 5 every 15 minutes (`senzuGiftAmount`, `senzuGiftCooldownTicks`), Shenron 16, Porunga 32, raids and quests. Carry them in a Senzu Bag.
- Stat capsules: +5 permanently to one stat by color (Red STR, Purple SKP and so on; `capsules.values` in general-server.json). A capped stat isn't raised and the capsule isn't used. Crafted from a Curative Flask, an Empty Capsule and farm plants. Mastery Capsule: +5% mastery to your active form.
- Medi-Bugs: Demon Realm items, including the beetles used for fusion and a worm that gives a Mastery Multiplier effect.
- Food: DMZ restores HP, Ki and stamina from food by hunger and saturation (`food` section: `healthPercentageRecoveredPerHungerPoint` and similar). `blacklistedItems` and `blacklistedNamespaces` exclude items.

Admin: `/dmzeffect give <effect> <duration> [targets]` (`-1` = permanent).

Keywords: senzu, senzu bean, senzu bag, korin, capsule, stat capsule, mastery capsule, medi bug, food, healing items, consumables
