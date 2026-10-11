# dragonminez-ai Context

Architecture facts. Keep them true: update this file when a structure below changes. Last checked 2026-10-11.

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
  - `openai_client` + `ai_tools` + `ai_guard` (AI calls, tools, output screening)
  - `ai_budget` (daily token budget, seeded from `support_traces`)
  - `tickets`, `ticket_transcripts`
  - `log_pages` (redact + render + 24h storage for uploaded logs). The `log_parser`/`dmzdebug` cogs post the cards; their `summary_for_ai` text is what the support AI sees for a log attachment (`ai_tickets._log_analyses`), never the raw file.
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
  - `knowledge/`
  - `message_templates.json` (rules and support-us templates)
  - `settings_overrides.json`
  - ticket transcripts (gitignored)
  - `log_pages/`: redacted log/dmzdebug viewer pages, expired by file mtime after 24h (`services/log_pages`, swept hourly by the `log_parser` cog; gitignored)
- AI support knowledge is an OpenAI vector store, synced from wiki.dragonminez.com by `scripts/sync_wiki_knowledge.py` (`.github/workflows/wiki-sync.yml`).

## Deploy

- DigitalOcean VPS, systemd unit `dragonminez-ai-discord.service`, run from a venv. Logs: `journalctl -u dragonminez-ai-discord.service`.
- Agents have no SSH access. Give the user commands to run.
- `self_update` cog: "Pull & restart" from Discord.
- `scripts/dmz-bot`: console CLI over a chmod-600 Unix socket (see Memory: VPS console).

## Build gate

Pushes to the mod repo that change `.java` files hit `/dmz-github-push` (needs `GITHUB_PUSH_WEBHOOK_SECRET`). Staff-devs approve in Discord, and then the bot dispatches the mod's `dev-jar-upload.yml` (workflow_dispatch only).
- Pushes to the same branch within 30 min fold into one prompt.
- The prompt auto-closes after 1h.
- `/buildjar` offers the last 3 pushes.
- `compile-check.yml` in the mod repo is the safety net.

## CI

`.github/workflows/ci.yml` runs pytest on Python 3.13. `codeql.yml` also runs.
