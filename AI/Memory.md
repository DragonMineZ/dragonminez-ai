# AI Memory: dragonminez-ai

Lasting decisions and prohibitions. This is not a changelog or task log: record only what changes future behavior, and delete entries once they are no longer true. Bruno is the owner; "the user" below means Bruno.

## Mentions (after the 2026-10-02 @everyone incident on the 100k server)

- The client default is `bot.SAFE_MENTIONS`: nobody gets pinged.
- The bot pings only the person it is answering, in tickets too: the first card of Bulma's answer in a ticket starts with a `<@requester>` line sent with `users=[that id]` only; later cards and edits never ping (Bruno, 2026-11-12). Only admins can ping anyone else (panel announcer, ADMIN tier).
- Every intended ping passes explicit ID lists (`users=[obj]`, `roles=[obj]`), never bare `True`. py-cord's `AllowedMentions(users=True)` leaves everyone/roles truthy.
- Bot-written text that names @everyone/@here goes in code spans.
- AI output passes `services/ai_guard.screen_reply`. AI replies may link only our own pages (`ai_guard.AI_LINK_PREFIXES` plus the wiki host).
- Guardrails are enforced in code first; prompt text is only a second layer.
- Tests: `test_bot_startup`, `test_ai_guard`.

## AI provider

- Claude via an Anthropic Console API key (`ANTHROPIC_API_KEY`), never a consumer Claude subscription (Pro/Max) token: those plans' terms don't allow powering a third-party service.
- Models: Claude Sonnet 5.5 and Haiku 5.5 only. No Opus (Bruno, 2026-10-17).
- Lean on Haiku: Sonnet only gets what Haiku at high effort couldn't settle (Bruno, 2026-10-24).
- No OpenAI code paths are kept.
- Spanish output (replies, translations) is Latin American, never Spain Spanish: ustedes, not vosotros (Bruno, 2026-10-24).
- `support_ai_traces` / `support_sessions` / `ticket_transcripts.openai_file_id` keep their OpenAI-era names as history. Don't rename them.
- The mod source tools (`list_mod_files` / `read_mod_file`) read only the allowlist in `services/mod_source.AREAS` (config classes, shipped configs and quests, commands and permissions, `en_us.json`) on `mod_source_branch` (default `main`, since v2.2 was merged into it). The AI quotes keys, values and commands from them and never pastes code.

## Language (Bruno, 2026-11-05)

- Every command, button and modal answers in the reader's language (en/es/pt) through `utils/i18n`: per-language tables (`TEXT`, `DESC`, ...) next to each module, `t(table, key, language)` falls back to English. A new language is only new table entries (plus `i18n.LANGUAGES` / `DISCORD_LOCALES`).
- Decision order (`user_language`): a single language role (`announcement_role_{en,es,pt}_id`) wins; several roles: the Discord locale if it is one of them, else the first in en, es, pt order; no role: the locale if supported (es-ES/es-419 → es, pt-BR → pt), else English. Roles are read off the main-server member, also in DMs.
- Reader's language: ephemeral replies, errors and denials, confirm prompts, modals, buttons on ephemeral cards, DMs (a mod DM and its Appeal button use the target's language).
- English: public results in shared channels (unless addressed to one person), mod-log, staff alert cards, staff-channel posts, the panel and AI prompts. Ticket-internal text keeps the ticket language.
- Command descriptions carry `description_localizations` (es-ES, es-419, pt-BR) from the same tables (`i18n.described`); command names are never localized.
- Spanish is Latin American (tú/ustedes), Portuguese Brazilian. Bruno-only and admin-only tools (`/database`, `/log-corpus`, VPS console, mail inbox) stay English.

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
- Card buttons that do slow work run through `ui/v2.while_busy`: the click disables the row and shows "⏳ Working…" at once, and a second click gets "Already working on it.". No silent `defer()` on buttons. Exception: buttons on a card many people share whose answer is ephemeral (the ticket welcome card's 📋 Summary) defer ephemerally with `invisible=False` (py-cord's "thinking" defer) instead, so the card isn't locked for everyone.
- Bulma's answer controls (`ask_requester`): only the requester can use them, through the same gates as typing. The edited card is the record of the answer (history and transcripts read it back). Specs live in the `ai_asks` table and modal submits are routed by custom id, never by in-memory callbacks. Upload asks (kind `file`) only go out in tickets and DMs: the bot reposts the files, so moderation never sees them. In public channels and `/ask` the AI tells people to open a ticket and post the log there for privacy (Bruno, 2026-10-20).
- `offer_action` buttons only run flows the requester could start themselves (get_staff = the "No, I still need help" escalation, open_ticket = the ticket panel's form, link_patreon = `/patreon link`, report_bug = a post in the bug-report forum, public channels and DMs only, never tickets; Bruno added it 2026-10-19), and only on their own click. Never add a privileged or new action there.
- "Ask Bulma" (message command) passes the same gates as typing there (ping, ticket or DM) and never pings; other bots' messages are refused. 👍/👎 feedback goes only under plain answers outside tickets (tickets have the "solved?" prompt), only the requester rates, first click wins.
- Threads: Bulma answers in public threads of our servers (forum posts too) like a public channel, never in private threads or the bug-report forum, and a thread is never a ticket. A follow-up to her answer in a text channel (a reply to it, or an answer on its controls) moves into a new thread; if the thread can't be made, she answers in the channel. In a help thread she opened, its requester is answered without a ping (who it is for is read off the thread's starter, never kept only in memory); everyone else still pings her.

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
- Bans (and timeouts) are appealed only through the appeals website (`appeals.` host) while `appeals_channel_id` is set; ban/timeout DMs link there. The old in-Discord appeal button and its flow are gone (2026-11-05); without the site a ban DM has no appeal button. Staff Accept/Deny on the card: first click wins (`WHERE outcome IS NULL`), Accept lifts through `perform()`. The bot never DMs the outcome; the site's status page is the answer.
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
- Language first (Bruno, 2026-11-05): the public panel is English plus one short ES/PT line. Picking a category opens its form in the member's language: their single language role (`announcement_role_{en,es,pt}_id`) opens it at once; no role or several get an ephemeral "Pick your language" card (🇬🇧/🇪🇸/🇧🇷, custom id `ticket_form:{category}:{lang}`, survives restarts) whose click opens the form. That language is the ticket's support language (no text detection, no in-ticket picker). Member-facing ephemeral replies of the flow are in it too. Strings live in per-language tables (`TEXT`, `CATEGORY_TEXT`); no `EN | ES` strings.
  - Everything member-facing inside a ticket and its DMs (welcome, Close button, solved prompt, deletion notice, transcript/solved/reminder DMs) uses the ticket's support language (en/es/pt) only; staff buttons and notices posted in the ticket stay English, while ephemeral replies to staff follow the reader's language (see Language).
  - Tickets opened without the panel's form language (a bug report's "Open a ticket"): the single role decides, otherwise it is detected from the form; no in-ticket picker card (Bruno, 2026-11-06). Owner or staff can switch it from the welcome card's select. The AI always answers in it.
- Categories, in panel order (Other last): Game-Breaking Bug, Patreon & Beta Access, Installation & Setup, Server & Multiplayer, Report a Member, Appeal a Punishment (warns and timeouts; bans go to the appeal website), Partnership & Creators, Contributing to the Mod (translations too), Other. The AI's first answer only runs for bug, patreon, install, server and other.
- Mod-only categories (`TicketCategory.mod_only`: Report a Member, Appeal a Punishment): Helpers get no access to the channel, open or closed; Moderators and up only. The AI is off in them from creation (the ticket's AI toggle, persisted); staff may turn it back on (Bruno, 2026-11-05).
- The AI's "Have I solved your problem?" prompt pings the owner. Escalation pings Helpers only, never Moderators, except in mod-only tickets, where it pings the moderator roles (`panel_moderator_role_ids`) since Helpers can't see them.
- Welcome card (Bruno, 2026-11-12): a greeting in the category's color with the owner's avatar and an "Opened … · language" line; the request card keeps the member's original answers first and, for es/pt tickets, an English translation for staff below it. The card is pinned (Discord's pin notice is deleted). Buttons: Close, AI toggle, 📋 Summary. There is no Solved button and no Draft reply.
- Staff copilot: Summary on the welcome card is staff-only (HELPER) and answers ephemerally. Draft reply is gone entirely (Bruno, 2026-11-12): Bulma never writes a staff member's reply for them. A signed public card (`-# ✍️ <@staff>`) is only posted by Staff Bulma's `ticket_reply` after the staff member confirms; history and transcripts read it as that staff member's turn, never Bulma's, and posting it counts as staff speaking (drops the pending AI answer, starts the owner-reply countdown).
- Code notes (what each push to the mod repo changed, `services/code_notes`) are staff-only: `/devlog` and Staff Bulma's `search_code_notes` tool, never in public answers, `/ask`, the ticket AI or log diagnosis, never posted to GitHub and never put in `knowledge_docs`.
- Messages in a ticket from anyone but its owner (staff, members added to it; never bots) that aren't in the ticket's language get translated automatically (`ai_ticket_translate_staff`, on by default; the name stays for stored overrides). The translation is posted through the ticket's webhook under the author's name and avatar, quoting the original, and the original is then deleted (Bruno, 2026-11-05) unless the card can't hold all of it (files, stickers, a poll, or over 1000 characters). If the webhook fails it goes out as Bulma's reply and the original stays. The owner's messages are translated the other way, for the staff present (Bruno, 2026-11-05): into the language (role) of everyone else who has written in the ticket, minus the message's own; several languages share one Haiku call and one card. Nobody with a language role has written yet, or only staff without one: no translation (no English guess). Off in Contributing and Partnership tickets (`TicketCategory.translate_owner`), whose teams share the owner's language (Bruno, 2026-11-11). Bulma's own answers are never translated: she answers in the asker's language. A card that replaced its original is read back as its author's turn in the original words; every other translation card stays out of AI history, text transcripts, summaries and knowledge.
- Owner reminder: 12h after someone else (staff, or an AI answer that isn't a handoff) spoke and the owner didn't reply, the owner gets one DM per silence (`tickets.awaiting_owner_since` / `owner_reminded_at`, swept by `_remind_owners`); the DM says the ticket closes after 24h. Still no owner reply 24h after that DM: the bot closes it for inactivity (auto-close On, normal transcript DM; `_close_inactive`) (Bruno, 2026-11-12). Neither the reminder nor the inactivity close touches a ticket channel staff moved out of the tickets category, or a hands-off category (`TicketCategory.hands_off`: Contributing, whose AI also starts off): those long discussions are left alone (Bruno, 2026-11-12, after the first inactivity sweep closed some).
- Testers can't see tickets.
- Re-Open and Delete are mod/admin only, from the closed card in the channel. Owners can never re-open. Exception: helpers may Delete a ticket with auto-close On (skips the 1h wait).
- Auto-close (Bruno, 2026-11-12): the closed card has an "Auto-close: Off/On" toggle (HELPER+) in place of the old Solved button. On means solved: `delete_at` = now + 1h (swept every minute by `_delete_solved`), the latest transcript row is marked resolved, and the close summary's support info suggestion (kept on `ticket_transcripts.support_info` at archive) goes to staff review once. Off clears the deadline and the resolved mark. Any re-open clears the deadline. A support info suggestion only ever comes from that toggle or from Staff Bulma's `ticket_solve`.
- Solved closes: the owner's Yes on the AI's prompt and the owner saying it's solved (AI off) go through `TicketsCog.solve_ticket` (Staff Bulma's `ticket_solve` too, with `staff=True`); inactivity closes post the card with auto-close On as well.
  - The ticket is closed (archived as resolved, same closed card) and the owner gets the solved DM: transcript link and when it gets deleted, no button. If the DM fails, a notice goes in the ticket channel.
  - It auto-deletes 1h later unless staff re-open it in that hour.
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
- The weekly "Answers to fix" cards (the week's 👎 answers, posted after the weekly report only, never by `/support-report`): what staff write there goes live at once as active support info with source `feedback`, since staff wrote it. The first staff Write or Skip claims the answer; a text that doesn't validate gives it back.
- Suggestions come only from tickets staff vouched for: the closed card's Auto-close turned On, or Staff Bulma's `ticket_solve` (the click is enough; staff needn't have typed). They keep the links/file names staff gave, and the AI may repeat links that appear in approved entries (`ai_guard.link_targets`).
- The daily post shows active temporary entries only, replaces the previous post, and skips the day when none are active.
- Managing entries is staff-level (`/info`, panel HELPER tier).

## Crash-log AI

- The AI diagnosis on log cards runs only in the main guild and never in tickets (the ticket AI already reads logs there). Per uploader per rolling 24h: members 3, patrons/testers 10, staff unlimited; admin-approved (🔍) logs don't count.

## Log corpus

- Every log the log parser analyzes in the main guild (tickets included) is kept to study and improve `utils/log_parser.py`: `services/log_corpus`, files in `data/log_corpus/` (gitignored, never committed).
- Temporary and redacted: `log_pages.redact` plus the chat around it with @user/@role/#channel labels, no Discord ids or names in the files. Files are deleted once their .zip is posted, or after 60 days.
- Only Bruno or admins download (the ping card's Download .zip); `/log-corpus` is Bruno-only. Only admins Exclude/Include. Bruno is the only one pinged, once per version at `log_corpus_ping_threshold`.
- The .zip goes to Bruno's PC for Claude Code sessions; delete it there once the analyzer work is done.
- Studying a corpus zip = run `scripts/log_corpus_eval.py`, add rules to `utils/crash_hints.py` and fixes to `data/knowledge/dmz22-crash-signatures.md` only for crashes staff answered (no guesses), then delete the zip and extracted files.

## Patreon beta whitelist

- `revoke_owner_access` never revokes: testers (`dev_jar_tester_role_ids`), anyone Patreon still reports active, or anyone whose lookup fails.
- `DragonMineZ/.github/allowed_betatesters.txt` has a `# Manually added` section above `# Auto-added`. The bot never removes names above that header and appends at the end, so `# Auto-added` stays the last section. No blank lines (the bot's rewrite drops them).

## Mail inbox

- The Hostinger mailbox (`MAIL_USER`) is admins-only: only main-guild admins reply from Discord, and `mail_channel_id` must be an admin-only channel (Bruno, 2026-11-05).
- The bot never changes the mailbox (no mark-read, move or delete); webmail stays the source of truth.

## Staff Bulma (Bruno, 2026-11-12)

- Staff (Helper+) only. Actions are only proposed; the staff member's click runs them through `perform()` with the caller's own caps, and the mod-log and audit read "via Bulma_AI". Never let the AI run an action directly.
- The vault needs consent, and onboarding is required to use it. It stores every message the person sends in channels the Member role can see, until `/bulma forget` (which deletes everything). Only the person's own vault is ever searched.
- Own Claude: staff connect their own Claude through the MCP endpoint (their client does the thinking). Never use a staff member's consumer subscription token in the bot.
- Excluded from Staff Bulma tools: `/database`, the VPS console, the mail inbox, the log corpus.
- Replies are short but not too short; no answer-style setting.

## Evals (Bruno, 2026-11-12)

- The support AI eval set (`eval_cases`) only grows from staff-vouched sources: Auto-close turned On by staff, Staff Bulma's solve, "Answers to fix" answers, approved support info. Never from owner-solved tickets, inactivity closes or AI output.
- Runs happen on the VPS through `scripts/support_eval.py`; the grader and the labeller are Haiku. Eval traces use workflow prefix `eval` and stay out of the support exports. Run files (`data/evals/runs/`) are gitignored.

## Kept on purpose

- The Dyno bot is gone, but `cogs/mod_log_sync.py` and the `dyno_*` settings stay until Bruno says to remove them. `mod_cases` rows with `source="dyno"` are history.
