-- ============================================================
-- docs RAG schema (idempotent – safe to run repeatedly)
-- ============================================================

CREATE TABLE IF NOT EXISTS docs (
    id          SERIAL PRIMARY KEY,
    source      TEXT        NOT NULL DEFAULT 'unknown',
    title       TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    lang        VARCHAR(5)  NOT NULL DEFAULT 'en',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS doc_embeddings (
    id          SERIAL PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    embedding   FLOAT8[] NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_docs_source ON docs (source);
CREATE INDEX IF NOT EXISTS idx_docs_lang   ON docs (lang);
