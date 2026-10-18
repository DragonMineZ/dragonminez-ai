# DMZ v2.2 alpha/beta builds: "You are not allowed to play this Beta/Alpha version"

Builds whose version contains -alpha or -beta (all v2.2 builds right now are 2.2-alpha) only let whitelisted Minecraft usernames play. Everyone else is disconnected on join with "You are not allowed to play this Beta/Alpha version" and a "Verify here" button that links Discord to request access automatically.

How it works: the server downloads the whitelist from DragonMineZ's GitHub (`allowed_betatesters.txt`) each time it starts (in singleplayer, each time the world opens). Names are matched case-insensitively. It applies on dedicated servers, LAN and singleplayer.

Getting access: patrons with beta access register their Minecraft username with `/patreon beta-access username:<name>` in the DragonMineZ Discord, or use the Verify button.

Already added but still kicked: the list is only fetched at startup, so restart the server (or reopen the world, in singleplayer) after being added. Also check the exact Minecraft username (not the Discord name), and that the server can reach raw.githubusercontent.com (no firewall blocking outgoing HTTPS).

Keywords: beta, alpha, whitelist, not allowed to play, kicked, beta access, patreon, verify here, allowed_betatesters, cannot join, disconnected on join, dev jar
