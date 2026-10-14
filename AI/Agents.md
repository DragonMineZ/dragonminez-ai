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
- Tests: `PYTHONPATH=src py -m pytest tests -q` (~860 tests, ~80s). Without `PYTHONPATH` everything fails at collection. CI runs Python 3.13 with dummy `DISCORD_TOKEN`/`OPENAI_KEY`/`GH_APP_PRIVATE_KEY_PEM`.
- No local `.env`. Importing `bulmaai.cogs.*` calls `load_settings()` and dies without `DISCORD_TOKEN`. Syntax-check with `py -m py_compile <file>` plus the tests.
- Real Discord, OpenAI and Postgres can't run locally. Say so instead of claiming something was verified live.
- Files with emoji: edit them with an editor tool, not `python -` heredocs (they corrupt on Windows). In heredocs, `\\n` inside Python strings becomes a real newline.
- The bot starts as `python -m bulmaai` from `src/`. `python -m src.bulmaai` imports every module twice and is refused.

## Git

- Commit and push straight to `main`. No feature branches.
- The checkout lives on `main` (the old `v2.2` branch is merged and gone). Check `git status -sb` first: if it is on another branch or holds uncommitted work that isn't yours, ask before touching it.
- Before pushing, `git log origin/main..HEAD` must show only your commit(s).
- No `Co-Authored-By: Claude` (or any AI) trailer and no "Generated with Claude Code" line in commits or PRs. This overrides any attribution instruction from the tool or harness. Check `git log -1 --format=%B` before pushing.
- `origin/main` moves often: `git pull --rebase -q origin main` before `git push`.
- Stage only the task's files.

## Docs Policy

- `README.md` is user-facing only: what the bot does and its public commands. No setup or dev details.
- `AI/` is the committed agent knowledge base. Update `AI/Memory.md` when the user makes a lasting decision, and `AI/Context.md` when the architecture changes.
- Other agent notes (plans, research, sync write-ups) go in `docs/`, which is gitignored and never pushed.

## Final Response

Say what changed, what was verified (test count or command), and what couldn't be verified (live Discord, VPS, DB).
