
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
