# DMZ v2.2 Saiyan racial skill: Zenkai and Limitless Power

Zenkai (from level 100): a lethal hit from an enemy leaves you at 1 HP instead, unless it dealt more than 100% of your max HP. You are knocked down 10 seconds, invulnerable, recovering 40% HP, 80% Ki and 80% stamina. Then STR, SKP and PWR rise by 1% to 10% depending on the hardest hit you took in the last 10 seconds. A hit of at least 25% of your max HP gives the full boost, which is permanent up to 3 times; other boosts last 120 to 240 seconds. Cooldown 900 seconds.

Limitless Power: each full-strength Zenkai raises your Power Release limit by 5%, up to +15%.

Config: `racialSkills.saiyan` in general-server.json (`enabled`, `minLevel`, `cooldownSeconds`, `knockoutSeconds`, `maxBuffPct`, `permanentMaxBuffs`, `buffStats`, `releaseBonusPerZenkai`, `releaseBonusCap`...). `racialSkills.enableRacialSkills` turns all racial skills off.

Reset racial progress: `/dmzracial reset [targets]` or the racial reset wish. Test it: `/dmztrigger <player> racialskill saiyan ...`.

Keywords: saiyan racial, zenkai, zenkai boost, revive at 1 hp, limitless power, power release, saiyan passive, racial skill, dmzracial
