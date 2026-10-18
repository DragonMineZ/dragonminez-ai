# DMZ v2.2 requirements, dependencies and incompatible mods

DragonMineZ 2.2 (builds named 2.2-alpha) runs only on Minecraft 1.20.1 with Forge 47.4.10 or newer and Java 17. Fabric, NeoForge and other Minecraft versions are not supported.

Required mods (the game refuses to start without them, on client and server):
- GeckoLib 4.8.3+ (1.20.1)
- TerraBlender 3.0.1.10+ (1.20.1)
- Curios API 5.14.1+ (1.20.1)

Declared incompatible (Forge blocks loading if installed): Legendary Tooltips, Epic Fight, Better Combat. Remove them, there is no config to make them work together.

Client and server must run the exact same DMZ jar. The network protocol includes a fingerprint of the mod's packet classes, so two different 2.2 dev builds will refuse to connect even if the version text looks the same. Fix: install the same build on both sides.

Alpha and beta builds (version contains -alpha or -beta) only let whitelisted Minecraft usernames join. See the beta whitelist doc.

Keywords: requirements, dependencies, forge version, java 17, geckolib, terrablender, curios, incompatible, epic fight, better combat, legendary tooltips, mismatch, version, protocol, cannot connect
