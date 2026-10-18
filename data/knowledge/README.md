DragonMineZ support knowledge lives in the bot's Postgres table `knowledge_docs`
(`services/knowledge.py`), searched with Postgres full-text search by the support AI.
Each doc has a `source`:

- `mediawiki`: every English content page of https://wiki.dragonminez.com, stored as
  `wiki--<Page_Title>.md`. The bot syncs it every 6 hours (`cogs/knowledge_sync.py`,
  reading `WIKI_API_URL`). Edit the wiki, not the stored docs; the sync replaces them.
- `ticket`: closed tickets that staff took part in and the summary marked as reusable.
- `faq`: the generated FAQ (`support-faq.md`), published with
  `scripts/suggest_support_faq.py --publish` (or `--publish-existing` after reviewing
  the Markdown it wrote).
- `local`: the `.md` files in this folder (this README excluded).

To add curated knowledge, drop a Markdown file here and commit it. The title is the
first `# ` heading (or the file name). The bot loads this folder on every start and
removes docs whose file was deleted.
