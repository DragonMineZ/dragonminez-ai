# DMZ v2.2 world boss rewards and contribution

Contribution = damage dealt (boss and minions) + damage mitigated (defense, blocking, ki barriers) + damage received + healing done (food, senzu and revives don't count). Your share = your points / everyone's. The HUD panel shows up to 8 fighters.

When the boss falls, contributors with at least 7.5% share roll every reward (`minContributionShare`); below that you get nothing. 100% rewards are guaranteed and scale with your points (top contributor full, everyone else at least 50%). Other rewards roll their chance plus a rank bonus: MVP +25%, 2nd +15%, 3rd +10%. Story difficulty doesn't change these rewards.

Default rewards (every boss pays Attributes, split like quests):
- Janemba: x500, Dimension Sword 50%, Dimensional Warp technique 20%.
- Turles: x50, Fruit of the Tree of Might 50%, Kill Driver 25%, Meteor Burst 15%.
- Metal Cooler Core: x105, 4 Gete Ingots, Supernova (Cooler) 20%, each Gete capsule 12%.
- Tamagami 3 / 2 / 1: x90 / x300 / x360, Hammer / Trident / Sword 30%. Each also drops a Demon Realm Dragon Ball.
- Gomah: x435, Demon Eye 2%.
Rewards are editable in `worldBoss.rewards.<boss>` (types ATTRIBUTES, ITEM, SKILL...; an empty list disables drops).

`/dmzworldboss results` (everyone) reopens the results screen for 30 minutes.

Keywords: world boss rewards, boss drop, drop chance, contribution, mvp, didnt get reward, 7.5%, dimension sword, demon eye, tamagami sword, dmzworldboss results
