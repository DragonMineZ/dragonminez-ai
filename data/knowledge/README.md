
DragonMineZ support knowledge is stored in the OpenAI vector store configured by
`OPENAI_SUPPORT_VECTOR_STORE_IDS`. The bot does not use a local/Postgres FAQ
database. Curated FAQ entries should be maintained as text-native knowledge files
and uploaded to the OpenAI Dashboard Storage vector store.

Recommended vector stores:

- `dragonminez-faq-prod` for short, approved FAQ answers generated from support traces
- `dragonminez-support-docs-prod` for longer docs, guides, PDFs, changelogs, and policy notes

The support bot can read both stores through:

```powershell
$env:OPENAI_SUPPORT_VECTOR_STORE_IDS='vs_faq,vs_support_docs'
```

The FAQ suggestion/publish script needs one upload target:

```powershell
$env:OPENAI_FAQ_VECTOR_STORE_ID='vs_faq'
$env:OPENAI_FAQ_SUGGESTION_MODEL='gpt-5.4-mini'
$env:OPENAI_FAQ_GENERATED_PATH='data/knowledge/generated/dragonminez-faq.md'
```

Recommended FAQ file shape:

```markdown
# DragonMineZ FAQ

## How do I transform?

Use the configured transform key after meeting the form requirements.
Tags: forms, controls
```

Generate candidate FAQ entries from recorded support traces:

```powershell
$env:PYTHONPATH='src'; python scripts/suggest_support_faq.py --limit 200 --output data/knowledge/generated/dragonminez-faq.md
```

Review and edit the generated Markdown before publishing. After approval, upload it
to the FAQ vector store without regenerating:

```powershell
$env:PYTHONPATH='src'; python scripts/suggest_support_faq.py --publish-existing
```

After updating FAQ or support docs, upload the changed file to the OpenAI vector
store named for production support knowledge, wait until processing is complete,
and keep the `vs_...` id in `OPENAI_SUPPORT_VECTOR_STORE_IDS`.

```powershell
$env:PYTHONPATH='src'; python scripts/export_support_eval_dataset.py --output data/evals/support_eval_dataset.jsonl
```

Preferred formats:
- `.md` or `.txt` for curated support notes, FAQs, changelogs, and troubleshooting docs
- `.json` for exported structured data such as tickets, Trello cards, or issue snapshots
- `.html` for copied wiki/help pages
- `.pdf` only when a text-native source is not available

Why text-native files are still preferred:
- text-native files preserve headings and structure better
- OpenAI file search can retrieve cleaner chunks
- admins can update individual files without rebuilding one large PDF export
