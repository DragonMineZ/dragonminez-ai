# dragonminez-ai Context

Architecture facts. Keep them true: update this file when a structure below changes. Last checked 2026-10-17.

## What it is

BulmaAI is the Discord bot for the DragonMineZ Minecraft mod community (main server ~100k members). It covers:
- AI support tickets
- bug reports
- crash-log parsing
- Patreon beta whitelist
- release/dev-jar announcements and the build gate
- moderation (it replaced Dyno)
- a staff web panel

The mod repo is a sibling checkout at `../../dragonminez` (GitHub `DragonMineZ/dragonminez`).

## Layout (`src/bulmaai/`)

- `bot.py`: the client. Sets `SAFE_MENTIONS`, runs schema setup on startup, and loads `config.DEFAULT_INITIAL_EXTENSIONS`.
- `config.py`: the `Settings` dataclass. Settings come from env vars, then panel overrides in `data/settings_overrides.json`. `web/settings_meta.py` describes which settings the panel can edit.
- `cogs/`: Discord commands, listeners and `tasks.loop` jobs. Keep them thin.
- `services/`: logic and DB access (asyncpg). Highlights:
  - `mod_actions` (all moderation actions)
  - `ai_client` (shared `AsyncAnthropic` client; the `anthropic` SDK is the only AI provider) + `ai_tools` + `ai_guard` (AI calls, tools, output screening). The support AI's fast pass is `claude-haiku-5-5`, escalation is `claude-sonnet-5-5`; both passes can use tools, including `search_knowledge`. `ai_oneshot` holds the single-shot helpers (summaries, translations, titles)
  - `ai_budget` (two daily token pools: small = haiku, big = all other models; seeded from `support_traces`)
  - `knowledge` (Postgres `knowledge_docs` table, full-text search; sources: mediawiki, ticket, faq, local `data/knowledge/*.md`)
  - `tickets`, `ticket_transcripts` (`knowledge_filename` links a transcript to its `knowledge_docs` row). `tickets.language` is the ticket's support language (member-facing text via `ui/ticket_views.TEXT`, AI via `ai_tickets._ticket_language`); `note_ticket_message` tracks the owner-reply countdown with one conditional UPDATE per ticket message
  - `support_info`: staff-curated entries (`support_info` table). Temporary ones (current issues, optional expiry) and permanent ones (custom answers); every active entry goes into the support AI's developer message (`ai_client._build_data_input`, 60s cache). `cogs/support_info.py` posts active temporary entries to `support_info_channel_id` every 24h (replacing the previous post; `support_info_post` row), runs `/info`, and owns the staff review card for AI suggestions (`finish_suggestion`). The ticket summary may carry a `support_info` suggestion; it is stored as `suggested` only on a staff Solved close. Panel page: `web/routes_support_info.py`.
  - `support_report`: weekly staff report (`cogs/support_report.py`, hourly loop, posts Mondays from 09:00 UTC to `support_report_channel_id`, none = off; `/support-report` builds it on demand, ephemeral). Numbers from `ticket_transcripts` and `support_ai_traces` (spend is an estimate of support replies only), plus one Sonnet call (`ai_support_escalation_model`) over the anonymized summaries and handed-off questions. The last post is the `support_report_posts` row, claimed atomically.
  - `log_pages` (redact + render + 24h storage for uploaded logs). The `log_parser`/`dmzdebug` cogs post the cards; their `summary_for_ai` text is what the support AI sees for a log attachment (`ai_tickets._log_analyses`), never the raw file.
  - Crash-log AI diagnosis: after posting a log card with errors, `log_parser._add_diagnosis` runs `ai_client.run_support_agent` on `summary_for_ai` (no tools but `search_knowledge`, `workflow_prefix="log_diagnosis"` so traces are `log_diagnosis`/`log_diagnosis_escalation`/`log_diagnosis_draft`, outside the support_* exports) and edits the card to add the "AI diagnosis" block. No placeholder; any skip or failure leaves the card as posted. Per-uploader rolling 24h limit in `log_ai_uses` (`services/log_ai_quota`, purged by the hourly `purge_log_pages` loop); settings `log_ai_*`.
  - `/dmzdebug` dumps: the mod (v2.2+, `DebugCommand`) writes `<player>.json` (`schemaVersion` 1, camelCase `party`/`quests` blocks, typed NBT under `sections`); v2.1 and older wrote the `<player>.log` text dump. `utils/dmzdebug_parser.py` reads both into one `DebugReport` (JSON `party`/`quests` are mapped to the text parser's snake_case shapes so `dmzdebug_diagnostics` handles both the same way).
  - `build_gate`, `patreon_*`
- `ui/`: V2 card builders. `v2.py` holds the shared helpers. `tests/v2_helpers.py` has `walk`/`texts`/`buttons` for asserting on cards.
- `web/`: the aiohttp staff panel (`server.py`, `routes_*.py`, `static/`). It runs on `127.0.0.1:8090` behind cloudflared and also serves ticket transcripts (`tickets.` host, `/t/<token>`), the 24h log viewer (`logs.` host, `/l/<token>`) and webhooks (Patreon, `/dmz-github-push`, release/dev-jar). The public hosts may only serve their own prefix (`server.security_middleware`).
- `github/`: GitHub App auth and API.
- `utils/`: `permissions.py` (is_admin / is_staff / is_bruno / in_main_guild), log parsers, language detection.
- `configs/prompts/`: support system prompt and facts.

## Data

- `scripts/schema.sql` runs on every startup (`services/db_schema.ensure_schema`). Schema changes are idempotent `CREATE ... IF NOT EXISTS` / `ALTER ... ADD COLUMN IF NOT EXISTS`. There are no migrations.
- `data/`:
  - `knowledge/`: local knowledge `.md` files, loaded into `knowledge_docs` (source `local`)
  - `message_templates.json` (rules and support-us templates)
  - `settings_overrides.json`
  - ticket transcripts (gitignored)
  - `log_pages/`: redacted log/dmzdebug viewer pages, expired by file mtime after 24h (`services/log_pages`, swept hourly by the `log_parser` cog; gitignored)
- AI support knowledge lives in `knowledge_docs`. `cogs/knowledge_sync.py` re-syncs wiki.dragonminez.com into it inside the bot every 6h.

## Deploy

- DigitalOcean VPS, systemd unit `dragonminez-ai-discord.service`, run from a venv. Logs: `journalctl -u dragonminez-ai-discord.service`.
- Agents have no SSH access. Give the user commands to run.
- `self_update` cog: a push webhook on this repo (`/dmz-bot-update`, signed with `GITHUB_PUSH_WEBHOOK_SECRET`, served by the release webhook server) triggers an immediate check of origin; a 10-minute poll is the fallback. Hot-reloadable commits apply on their own; the rest wait for "Pull & restart" from Discord.
- `scripts/dmz-bot`: console CLI over a chmod-600 Unix socket (see Memory: VPS console).

## Build gate

Pushes to the mod repo that change `.java` files hit `/dmz-github-push` (needs `GITHUB_PUSH_WEBHOOK_SECRET`). Staff-devs approve in Discord, and then the bot dispatches the mod's `dev-jar-upload.yml` (workflow_dispatch only).
- Pushes to the same branch within 30 min fold into one prompt.
- The prompt auto-closes after 1h.
- `/buildjar` offers the last 3 pushes.
- `compile-check.yml` in the mod repo is the safety net.

## CI

`.github/workflows/ci.yml` runs pytest on Python 3.13. `codeql.yml` also runs.
