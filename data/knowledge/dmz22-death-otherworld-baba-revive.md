# DMZ v2.2 death, Otherworld, Fortuneteller Baba and reviving

With `worldGen.otherworldActive` on (default), dying sends your spirit to the Otherworld arrival point with a halo instead of a normal respawn. You keep your abilities, don't get hungry, and earn 2x TP while dead. Forms, Kaioken, effects and fusion end on death.

Getting back to the living world:
1. Talk to Fortuneteller Baba in the Otherworld cloud realm (near King Yemma). Her service has a cooldown after each death (`reviveCooldownSeconds` 180, shown live).
   - Default (`babaTemporaryRevive` true): you stay dead with your halo but borrow 1 hour (`babaTempReturnSeconds`). The timer pauses in the Otherworld or while knocked out, and runs faster in some worlds (Nether x2, End x3, Time Chamber x5), while transformed, and x1.5 per extra return (max x5). A lethal hit knocks you out instead of killing you and costs part of your time (/kill or the void still kill). When time runs out you return to the Otherworld and must wait one Overworld day (sleeping helps) before Baba can send you again.
   - With `babaTemporaryRevive` false she revives you for good.
2. Talk to King Yemma to travel down (to your spawn point).

Permanent revival: the revive wish (Shenron or Porunga) or `/dmzrevive [targets]`; halo with `/dmzhalo`. `babaHardcoreEnabled` makes the cooldown grow per death. Turning `otherworldActive` off means vanilla respawn.

Keywords: death, died, dead, otherworld, baba, fortuneteller baba, king yemma, revive, respawn, halo, borrowed time, temporary revive, timer, stuck in otherworld, how to get back to earth, dmzrevive
