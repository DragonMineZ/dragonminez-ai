# Patreon Discord Role Linking Guide

## When to apply this guide

Use this guide when a user:
- Says they are a Patreon supporter but has no Patreon role in the DragonMineZ Discord server
- Gets an error from `/beta-access` or `/gift-beta` saying they need a Patreon beta access role
- Says their Patreon perks or Discord role are missing
- Cannot access Patreon-only channels or commands despite being a patron

## Root cause

Patreon does not automatically give Discord roles unless the user has **linked their Patreon account to Discord** through Patreon's Connected Apps settings. Without this link, Patreon cannot identify the user in Discord and cannot grant the role.

## Solution: Link Patreon to Discord

Direct the user to the official Patreon guide:
https://support.patreon.com/hc/en-us/articles/212052266-Getting-Discord-access

### Steps summary
1. Log in at patreon.com
2. Go to **Settings → Connected Apps** (or account settings)
3. Click **Connect** next to Discord
4. Authorize the connection in Discord
5. Patreon will automatically sync the tier role to supported Discord servers within a few minutes

## After linking

Once linked, the user should:
- Receive their Patreon tier role in the DragonMineZ Discord server automatically
- Be able to run `/beta-access <MinecraftUsername>` to register for beta whitelist access
- Contact staff if the role does not appear after a few minutes

## Important notes

- The user must be a **currently active** Patreon member at an eligible tier for the role to be granted
- If the role still does not appear after linking, ask them to check whether their Patreon pledge is active and at the correct tier
- Staff can verify Patreon link status through the `/database` command if needed

Tags: patreon, discord, role, linking, beta access, no role, missing role, perks, connected apps, oauth
