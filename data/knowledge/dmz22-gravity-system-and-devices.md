# DMZ v2.2 gravity and the Gravity Device

Raw gravity = the highest of your dimension's gravity (Earth, Namek, Otherworld 1; Nether 8; Time Chamber 10; End 20), a WorldGuard region's `dmz-gravity` flag, or 10x within 100 blocks of King Kai; plus an active Gravity Device room on top (50x adds 49). Your stats reduce it to net gravity (they only cancel much at very high levels).

Per point of net gravity: +2.5% TP, +0.25% form mastery (hit mastery), stronger weight load; penalties of -1% STR, SKP, PWR, DEF, STM (max 90%), slower movement, attacks, jumps and flight (max 90-95%), +2% fall damage (max 60%), +4% attack stamina cost. At 75 net gravity flight stops and the gravity bonuses drop to nothing. In the Time Chamber its own gravity doesn't count for the TP bonus (it has its own x1.75), but a device inside does.

Gravity Device: craft from repeaters, iron blocks, an anvil, a weight item and an Earth Radar's CPU. Place it in a sealed room 5 to 25 blocks on every axis (full blocks, doors or trapdoors seal; slabs, stairs, fences don't), power it with Star Energy from a Fuel Generator through Energy Cables (20,000 buffer, 1 energy per second per x of gravity), set 1x to 1000x and Turn On. "Invalid room (5-25 blocks each side)" means a gap or wrong size.

Config: `gravity` section (`gravityPerWorld`, `npcGravity*`, `device*`, `hardStopThreshold`, `tp*`). Weights are covered in their own doc.

Keywords: gravity, gravity device, gravity machine, gravity room, invalid room, star energy, fuel generator, gravity training, net gravity, slow, cant fly, heavy, gravityPerWorld, worldguard gravity
