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

## AI provider

- Claude via an Anthropic Console API key (`ANTHROPIC_API_KEY`), never a consumer Claude subscription (Pro/Max) token: those plans' terms don't allow powering a third-party service.
- Models: Claude Sonnet 5.5 and Haiku 5.5 only. No Opus (Bruno, 2026-10-17).
- No OpenAI code paths are kept.
- `support_ai_traces` / `support_sessions` / `ticket_transcripts.openai_file_id` keep their OpenAI-era names as history. Don't rename them.

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
- Card buttons that do slow work run through `ui/v2.while_busy`: the click disables the row and shows "⏳ Working…" at once, and a second click gets "Already working on it.". No silent `defer()` on buttons.
- Bulma's answer controls (`ask_requester`): only the requester can use them, through the same gates as typing. The edited card is the record of the answer (history and transcripts read it back). Specs live in the `ai_asks` table and modal submits are routed by custom id, never by in-memory callbacks.

## Moderation

- `services/mod_actions.perform()` is the single action path. New settings are dataclass defaults, editable from the panel.
- Slash commands only, no prefix commands.
- Warn ladder: 2 in 7d = 24h timeout, 5 in 30d = 3d, 7 in 30d = ban.
  - Capped by the warner: helpers escalate to timeout at most; moderators and up can kick or ban; automod has the full ladder.
  - Skipped steps are noted on the case.
- everyone_ping: delete-only on the first tries; the 3rd try within 10 min is a warn strike.
- Verbal warnings: an automod severity (any filter's "verbal" rule) stored as `mod_cases` action `verbal`. They never count toward the ladder; 3 active verbals within 1h become one automod warn strike, and those verbals are ended when they convert.
- staff_ping: server-wide filter (typed mentions of HELPER+ members or staff roles), default severity verbal, lowest priority. It replaced the ticket-only staff-ping warn.
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
- The ticket panel, forms and ephemeral replies stay EN | ES. Everything member-facing inside a ticket and its DMs (welcome, solved prompt, deletion notice, transcript/solved/reminder DMs) uses the ticket's support language (en/es/pt) only; staff-only buttons and notices stay English.
  - Support language: the owner's single language role (`announcement_role_{en,es,pt}_id`). No role: detected from the form. Several: a picker card asks them. Owner or staff can switch it from the welcome card's select. The AI always answers in it.
- The AI's "Have I solved your problem?" prompt pings the owner. Escalation pings Helpers only, never Moderators.
- Owner reminder: 12h after someone else (staff, or an AI answer that isn't a handoff) spoke and the owner didn't reply, the owner gets one DM per silence (`tickets.awaiting_owner_since` / `owner_reminded_at`, swept by `_remind_owners`).
- Testers can't see tickets.
- Re-Open and Delete are mod/admin only, from the closed card in the channel. Owners can never re-open. Exception: helpers may Delete a ticket closed as Solved (skips the 1h wait).
- Solved closes: the staff "Solved" button, the owner's Yes on the AI's prompt, and the owner saying it's solved (AI off) all go through `TicketsCog.solve_ticket`. Only the staff click may produce a support info suggestion.
  - The ticket is closed (archived as resolved, same closed card) and the owner gets the solved DM: transcript link and when it gets deleted, no button. If the DM fails, a notice goes in the ticket channel.
  - It auto-deletes 1h later (`tickets.delete_at`, swept every minute by `_delete_solved`) unless staff re-open it in that hour. Any re-open clears the deadline.
  - Solved DMs sent before this still have a Re-open button; it only answers, in the owner's language, that staff re-open tickets and they can open a new one.
- Every deletion (Delete button, the solved sweep, owner left) goes through `delete_ticket`: a "🗑️ This ticket will be deleted <t:…:R>" notice in the channel, `delete_at` = now + 10s stored first (a restart mid-wait is finished by the sweep), 10s wait, then delete. A staff re-open during those 10s wins.
- One open ticket per user. A second attempt from the panel is refused with a link to the open one.
- Every close (button, `/ticket close`, owner left) makes a transcript; there is no "close without transcript". They're hosted at `tickets.dragonminez.com/t/<token>` by the panel's aiohttp app.
  - One address per ticket: re-open and re-close overwrite the same file and DB row (same token). A page staff made permanent stays permanent.
  - The closed card's transcript button shows "Saving transcript…", then links to the page (or "Retry transcript" if archiving failed). No separate "transcript saved" message in the channel.
  - Messages sent while the ticket was closed (except the bot's own) never reach any transcript, summary or knowledge file; one "N messages were sent while this ticket was closed." notice stands in per closed stretch (`services/tickets.hide_closed_messages`, stretches from `tickets.closed_periods` + `closed_at`).
- Modal-submit `defer()` defaults to `invisible=True`, which edits the panel message publicly.
- Game-breaking bug reports (crash, corrupted world, can't load or join) get an "Open a ticket" button on the triage card.
  - Only the reporter can click it, or staff opening the ticket for the reporter.
  - The post's text and files move into a bug ticket owned by the reporter, and the forum thread is deleted (status `ticket`).

## Support info

- Nothing the AI suggests goes live without a staff click (Approve / Edit & approve on the review card, or the panel). Approved temporary suggestions without an expiry expire after 7 days.
- Suggestions come only from tickets staff closed with Solved (the click is enough; staff needn't have typed). They keep the links/file names staff gave, and the AI may repeat links that appear in approved entries (`ai_guard.link_targets`).
- The daily post shows active temporary entries only, replaces the previous post, and skips the day when none are active.
- Managing entries is staff-level (`/info`, panel HELPER tier).

## Crash-log AI

- The AI diagnosis on log cards runs only in the main guild and never in tickets (the ticket AI already reads logs there). Per uploader per rolling 24h: members 3, patrons/testers 10, staff unlimited; admin-approved (🔍) logs don't count.

## Log corpus

- Every log the log parser analyzes in the main guild (tickets included) is kept to study and improve `utils/log_parser.py`: `services/log_corpus`, files in `data/log_corpus/` (gitignored, never committed).
- Temporary and redacted: `log_pages.redact` plus the chat around it with @user/@role/#channel labels, no Discord ids or names in the files. Files are deleted once their .zip is posted, or after 60 days.
- Only Bruno or admins download (the ping card's Download .zip); `/log-corpus` is Bruno-only. Only admins Exclude/Include. Bruno is the only one pinged, once per version at `log_corpus_ping_threshold`.
- The .zip goes to Bruno's PC for Claude Code sessions; delete it there once the analyzer work is done.

## Patreon beta whitelist

- `revoke_owner_access` never revokes: testers (`dev_jar_tester_role_ids`), anyone Patreon still reports active, or anyone whose lookup fails.
- `DragonMineZ/.github/allowed_betatesters.txt` has a `# Manually added` section above `# Auto-added`. The bot never removes names above that header and appends at the end, so `# Auto-added` stays the last section. No blank lines (the bot's rewrite drops them).

## Kept on purpose

- The Dyno bot is gone, but `cogs/mod_log_sync.py` and the `dyno_*` settings stay until Bruno says to remove them. `mod_cases` rows with `source="dyno"` are history.
