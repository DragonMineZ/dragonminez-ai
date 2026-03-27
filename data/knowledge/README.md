Drop support knowledge files here and ingest them with:

```powershell
python scripts/ingest_docs.py upload data/knowledge --lang en --source knowledge
```

Preferred formats:
- `.md` or `.txt` for curated support notes, FAQs, changelogs, and troubleshooting docs
- `.json` for exported structured data such as tickets, Trello cards, or issue snapshots
- `.html` for copied wiki/help pages
- `.pdf` only when a text-native source is not available

Why this is better than PDF-only uploads:
- text-native files preserve headings and structure better
- chunking is cleaner, so retrieval is more accurate
- admins can update individual files without rebuilding one large PDF export
