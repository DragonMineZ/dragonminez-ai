-- ============================================================
-- docs RAG schema  (run once, or re-run – all statements are
-- idempotent thanks to IF NOT EXISTS)
-- ============================================================

-- Stores documentation chunks
CREATE TABLE IF NOT EXISTS docs (
    id          SERIAL PRIMARY KEY,
    source      TEXT        NOT NULL DEFAULT 'unknown',   -- e.g. "wiki_en.pdf"
    title       TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    lang        VARCHAR(5)  NOT NULL DEFAULT 'en',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stores the OpenAI embedding for each doc chunk
CREATE TABLE IF NOT EXISTS doc_embeddings (
    id          SERIAL PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    embedding   FLOAT8[] NOT NULL
);

-- Speeds up the "delete old source" + "fetch by lang" paths
CREATE INDEX IF NOT EXISTS idx_docs_source ON docs (source);
CREATE INDEX IF NOT EXISTS idx_docs_lang   ON docs (lang);

