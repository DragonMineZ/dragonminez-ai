# DMZ v2.2 structures, how they generate, /dmzlocate ids

Each master structure is placed once per world, only in its biomes, fitted to the terrain. Unique structures added in an update (Baba's Palace, Tree of Might, Big Gete Star...) still appear in old worlds when the planned chunk loads; chunks where players spent over an hour are skipped. Saiyan Craters only appear in new chunks. Toggle: `worldGen.generateCustomStructures`; distances `structureMinDistanceFromSpawn` 500, `structureMaxDistanceFromSpawn` 10000, `structureMinDistanceBetween` 500.

`/dmzlocate <id>` (dragonminez.dmzlocate) finds the nearest one in your dimension, with clickable coordinates; if none, it says which dimension and biomes to explore. Ids: goku_house, roshi_house, kamilookout, piccolo_house, buu_house, yamcha_house, gero_lab, baba_palace, cell_arena, babidi, trunks_ship, vegeta_pod, rrtower, red_ribbon_base (Red Ribbon raid site), tree_of_might, saiyan_crater, frieza_ship, elder_guru, village_ajissa, village_sacred, namek_ruins, gete_star, oldkai_pillar, demon_village, gomah_camp, gomah_cradle, otherworld_tournament (Otherworld only). World boss lairs: `/dmzworldboss locate`. Missing structure: `/place structure dragonminez:<id>`. NPC traders also sell treasure maps.

Protection: master homes and the Time Chamber resist ki unless `allowKiGriefingMasterStructures`; tournament arenas can't be built in or broken unless `allowBuildingInArenaStructures`.

Keywords: structures, structure, worldgen, goku house, kame house, kami lookout, gero lab, cell arena, structure not spawning, cant find, locate, dmzlocate, structure ids, place structure, treasure map
