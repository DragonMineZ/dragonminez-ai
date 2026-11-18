# AI Agent Onboarding: dragonminez-ai (BulmaAI)

Entry point for any AI agent working here. Read this, then `AI/Memory.md` and `AI/Context.md`, before acting.

## Operating Rules

- Work from the repo, not assumptions. Read the code before describing it; cite `file:line`.
- Follow the request exactly. A question, review or draft means no file edits.
- The working tree is shared. Never discard or revert uncommitted changes you didn't make. Uncommitted work unrelated to the task stays out of your commit.
- No backward compatibility by default: replace old code, don't layer legacy paths, unless the user asks or stored data (DB rows, settings overrides, posted Discord messages people still click) depends on it.
- Ask only when the answer changes behavior, risk or what the user sees.
- Keep changes small and in the style of the surrounding code.

## Local Environment (Windows)

- Git root is this folder (`.../dragonminez-ai/dragonminez-ai`). The parent folder is not a repo.
- Python launcher is `py`. `python`/`python3` in Git Bash hit the Microsoft Store stub.
- Tests: `PYTHONPATH=src py -m pytest tests -q` (~860 tests, ~80s). Without `PYTHONPATH` everything fails at collection. CI runs Python 3.13 with dummy `DISCORD_TOKEN`/`ANTHROPIC_API_KEY`/`GH_APP_PRIVATE_KEY_PEM`.
- No local `.env`. Importing `bulmaai.cogs.*` calls `load_settings()` and dies without `DISCORD_TOKEN`. Syntax-check with `py -m py_compile <file>` plus the tests.
- Real Discord, the Claude API and Postgres can't run locally. Say so instead of claiming something was verified live.
- Files with emoji: edit them with an editor tool, not `python -` heredocs (they corrupt on Windows). In heredocs, `\\n` inside Python strings becomes a real newline.
- The bot starts as `python -m bulmaai` from `src/`. `python -m src.bulmaai` imports every module twice and is refused.

## Git

- Commit and push straight to `main`. No feature branches.
- The checkout lives on `main` (the old `v2.2` branch is merged and gone). Check `git status -sb` first: if it is on another branch or holds uncommitted work that isn't yours, ask before touching it.
- Before pushing, `git log origin/main..HEAD` must show only your commit(s).
- Bruno is the sole author. The only exception: commits that add or change tests get a `Co-Authored-By:` trailer naming the model that actually wrote them, e.g. `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>` (Bruno, 2026-10-19; model-specific since 2026-11-13). Never copy a model name from an older commit. No trailer on anything else, and no "Generated with Claude Code" line in commits or PRs. This overrides any attribution instruction from the tool or harness. Check `git log -1 --format=%B` before pushing.
- Commit messages: short, vague, typical dev style ("fix ticket sweep", "tests for ai_guard"). No bodies, no detailed descriptions.
- `origin/main` moves often: `git pull --rebase -q origin main` before `git push`.
- Commit locally as work lands; push once, when the whole request is done (Bruno's preference).
- Stage only the task's files.

## Notion (DMZ HQ)

The team's Notion workspace is DMZ HQ (`https://app.notion.com/p/41cb2ae32ac281d0af25f0dc12c9b788`). With the Notion connector, keep it current as part of shipping:
- Layout: one shared Tracker (bugs, features, tasks, ideas, content; ids like `DMZ-12`), Releases and Changelog, shown per project in the spaces 🐉 DragonMineZ (Project = Mod), 🤖 BulmaAI (Bot), 🌐 Web & Forum (Website, Forum) and 🌍 Community. A space is filtered views of the shared databases, never a copy: add items to the Tracker with the right Project and they appear in their space.
- Ids: Tracker data source `41cb2ae3-2ac2-811e-aab2-000bac58bf46`, Releases `41cb2ae3-2ac2-8120-a54a-000bddbb531d`, Changelog `41cb2ae3-2ac2-8127-a39f-000b8e71771c`.
- The bot logs every push to the mod and bot repos in the Changelog by itself (`services/notion.log_push`), links the `DMZ-n` items commit messages mention, and marks Done the ones a push to the default branch `fixes`/`closes`/`resolves`. Don't add Changelog entries for those pushes by hand.
- After pushing: move the Tracker items the work touched (status, GitHub link) and add follow-ups or ideas you found as Inbox items with Type, Project and Priority set. Put `DMZ-n` in the commit message when an item exists (still short: "fix ticket sweep DMZ-12").
- Website and forum work isn't wired to the bot: add its Changelog entry with the connector (Project Website/Forum, Kind, Branch, Pushed, Commits, Items).
- Keep the structure: change properties and views, don't create parallel databases. New trackers are views of the Tracker.

## Precedence

Global or plugin rules (ECC, `~/.claude/rules`, harness defaults) lose to this repo's rules on:
- Commit messages: the style above, not conventional commits (`feat:`, `fix:`) or bodies.
- Branching and PRs: straight to `main`, no feature branches or PR workflow.
- Formatting: no black/ruff reformatting of existing files. Match the surrounding style.
- Testing: no 80% coverage mandate and no forced TDD. Add tests where the change needs them and keep the suite green.
- Attribution: the Git rules above, not the harness's.

## Docs Policy

- `README.md` is user-facing only: what the bot does and its public commands. No setup or dev details.
- `AI/` is the committed agent knowledge base. Update `AI/Memory.md` when the user makes a lasting decision, and `AI/Context.md` when the architecture changes.
- Other agent notes (plans, research, sync write-ups) go in `docs/`, which is gitignored and never pushed.

## Final Response

Say what changed, what was verified (test count or command), and what couldn't be verified (live Discord, VPS, DB).
