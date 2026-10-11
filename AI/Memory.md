# AI Memory: dragonminez-ai

Lasting decisions and prohibitions. This is not a changelog or task log: record only what changes future behavior, and delete entries once they are no longer true. Bruno is the owner; "the user" below means Bruno.

## Mentions (after the 2026-10-02 @everyone incident on the 100k server)

- The client default is `bot.SAFE_MENTIONS`: nobody gets pinged.
- The bot pings only the person it is answering. Only admins can ping anyone else (panel announcer, ADMIN tier).
- Every intended ping passes explicit ID lists (`users=[obj]`, `roles=[obj]`), never bare `True`. py-cord's `AllowedMentions(users=True)` leaves everyone/roles truthy.
- Bot-written text that names @everyone/@here goes in code spans.
- AI output passes `services/ai_guard.screen_reply`. AI replies may link only our own pages (`ai_guard.AI_LINK_PREFIXES` plus the wiki host).
- Guardrails are enforced in code first; prompt text is only a second layer.
- Tests: `test_bot_startup`, `test_ai_guard`.

## Discord UI: Components V2 only

- Every bot message is native V2, built with `ui/v2.py` (card, head, facts, fit, trim, rows). Never send `discord.Embed` or a classic `discord.ui.View`.
- Interactive prompts are `DesignerView` subclasses. Persistent buttons are routed by custom id in the cog's `on_interaction`.
- No legacy support for pre-V2 posts.
- Plain-text ephemeral errors stay plain text.
- V2 text limit is 4000 characters per message. Use `fit` and `trim`.
- Visibility rule: change only *where* a message shows (public or ephemeral), never who can run something.
  - Errors and denials are ephemeral.
  - Destructive commands (`/clearwarns`, `/purge`, `/lockdown start`) show an ephemeral confirm prompt, then a public result plus a mod-log entry.
  - Mod replies don't mention whether the DM was delivered.
- Alerts about deleted messages never show "Jump to message". Quote the captured text instead.
- Handled alerts collapse (`alert_card` in `ui/mod_cards.py`).

## Moderation

- `services/mod_actions.perform()` is the single action path. New settings are dataclass defaults, editable from the panel.
- Slash commands only, no prefix commands.
- Warn ladder: 2 in 7d = 24h timeout, 5 in 30d = 3d, 7 in 30d = ban.
  - Capped by the warner: helpers escalate to timeout at most; moderators and up can kick or ban; automod has the full ladder.
  - Skipped steps are noted on the case.
- everyone_ping: delete-only on the first tries; the 3rd try within 10 min is a warn strike.
- Code blocks are exempt from the emoji and newline filters (users paste crash logs).
- The excessive-caps filter was removed on 2026-10-11 at Bruno's request. Don't re-add it.
- Scam images stay in shadow mode (`moderation_scam_images_enforce=False`) until the user says otherwise. Learning happens only on an explicit staff click.
- Grants work only in the main guild (and the dev guild) (`permissions.in_main_guild`).
  - `/database` is Bruno-only.
  - GitHub PR merge/close needs panel ADMIN; issue actions need staff.
- Known gaps, left on purpose: Cyrillic confusables in banned words, replay protection on `/dmz-release` and `/dmz-dev-jar`, panel session revocation, XP for automod-deleted messages.

## Anything that must survive a restart

Timers, pending prompts and alert outcomes never live in memory or `asyncio.sleep`. Use a DB row with `expires_at`/`outcome` plus a `tasks.loop` sweep (e.g. `joiner_alerts`, `mod_commands.expire_tempbans`). Staff clicks and the sweep race on `... WHERE outcome IS NULL`; the first one wins.

## VPS console (`dmz-bot`)

Console actions are private to Bruno:
- `perform(..., record=False)`: no case, no mod-log post, no panel trace, no "via console" tag.
- Report only to channel `1557490771328770198` (Bruno's personal server).
- Never log them at WARNING or above (`discord_log_forwarding` sends those to staff). Use INFO.

## Tickets

- In-house tickets (`cogs/tickets.py`). Ticket Tool is gone and its compat code is deleted. Don't re-add it.
- Strings are EN | ES only (no PT). Inside tickets everything is English.
- Testers can't see tickets.
- Re-Open and Delete are mod/admin only.
- Max 2 open tickets per user.
- Transcripts are made only via the staff Transcript button or AI auto-solve, never on close or delete. They're hosted at `tickets.dragonminez.com/t/<token>` by the panel's aiohttp app.
- Modal-submit `defer()` defaults to `invisible=True`, which edits the panel message publicly.

## Patreon beta whitelist

- `revoke_owner_access` never revokes: testers (`dev_jar_tester_role_ids`), anyone Patreon still reports active, or anyone whose lookup fails.
- `DragonMineZ/.github/allowed_betatesters.txt` has a `# Manually added` section above `# Auto-added`. The bot never removes names above that header and appends at the end, so `# Auto-added` stays the last section. No blank lines (the bot's rewrite drops them).

## Kept on purpose

- The Dyno bot is gone, but `cogs/mod_log_sync.py` and the `dyno_*` settings stay until Bruno says to remove them. `mod_cases` rows with `source="dyno"` are history.
