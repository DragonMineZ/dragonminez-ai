# dragonminez-ai
WIP AI Discord Bot Written in Python

## Patreon announcements

The bot polls Patreon campaign posts and announces them in a public Discord channel.

Default behavior:

- Uses Patreon API v2 `campaigns/{campaign_id}/posts`
- Requires a creator token with the `campaigns.posts` scope
- Posts title/link-only announcements so Discord does not mirror the full Patreon post body
- Defaults to the configured public sneak-peeks channel unless `PATREON_ANNOUNCEMENT_CHANNEL_ID` is overridden
- Seeds the latest post on first boot so it does not backfill old announcements

Optional environment overrides:

```dotenv
PATREON_CREATOR_TOKEN=<creator access token with campaigns.posts scope>
PATREON_CAMPAIGN_ID=<patreon campaign id>
PATREON_ANNOUNCEMENT_CHANNEL_ID=<public discord channel id>
```

## CurseForge updates

The bot now watches the DragonMineZ CurseForge project and announces new files automatically.

Default behavior:

- Tracks project `1136088` (`minecraft/mc-mods/dragonminez`)
- Polls every `15` minutes
- Posts release updates to the configured releases channel
- Seeds the current file on first boot so it does not backfill old announcements

Optional environment overrides:

```dotenv
CURSEFORGE_ENABLED=true
CURSEFORGE_PROJECT_ID=1136088
CURSEFORGE_PROJECT_SLUG=minecraft/mc-mods/dragonminez
CURSEFORGE_ANNOUNCEMENT_CHANNEL_ID=<discord channel id>
CURSEFORGE_POLL_MINUTES=15
CURSEFORGE_API_KEY=<optional official CurseForge API key>
```

Without `CURSEFORGE_API_KEY`, the watcher uses the public CFWidget API. When a key is provided, it upgrades to the official CurseForge API for richer release data such as changelogs and direct download URLs.
