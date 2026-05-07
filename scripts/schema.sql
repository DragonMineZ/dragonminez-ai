-- ============================================================
-- docs RAG schema (idempotent – safe to run repeatedly)
-- ============================================================

CREATE TABLE IF NOT EXISTS docs (
    id          SERIAL PRIMARY KEY,
    source      TEXT        NOT NULL DEFAULT 'unknown',
    source_type TEXT        NOT NULL DEFAULT 'text',
    section     TEXT,
    chunk_index INTEGER     NOT NULL DEFAULT 0,
    title       TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    content_hash TEXT       NOT NULL DEFAULT '',
    lang        VARCHAR(5)  NOT NULL DEFAULT 'en',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS doc_embeddings (
    id          SERIAL PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    embedding   FLOAT8[] NOT NULL,
    model       TEXT NOT NULL DEFAULT 'text-embedding-3-large',
    dimensions  INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE docs ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'text';
ALTER TABLE docs ADD COLUMN IF NOT EXISTS section TEXT;
ALTER TABLE docs ADD COLUMN IF NOT EXISTS chunk_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE docs ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE docs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE doc_embeddings ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT 'text-embedding-3-large';
ALTER TABLE doc_embeddings ADD COLUMN IF NOT EXISTS dimensions INTEGER NOT NULL DEFAULT 0;

UPDATE docs
SET content_hash = md5(coalesce(source, '') || '|' || coalesce(title, '') || '|' || coalesce(content, ''))
WHERE content_hash = '';

UPDATE doc_embeddings
SET dimensions = coalesce(array_length(embedding, 1), 0)
WHERE dimensions = 0;

CREATE INDEX IF NOT EXISTS idx_docs_source ON docs (source);
CREATE INDEX IF NOT EXISTS idx_docs_lang   ON docs (lang);
CREATE INDEX IF NOT EXISTS idx_docs_updated_at ON docs (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_docs_search_vector
    ON docs
    USING GIN (to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, '')));

CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_source_lang_hash
    ON docs (source, lang, content_hash);

CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_embeddings_doc_id
    ON doc_embeddings (doc_id);

-- ============================================================
-- FAQ knowledge schema (separate from docs RAG tables)
-- ============================================================

CREATE TABLE IF NOT EXISTS faq_entries (
    id                          SERIAL PRIMARY KEY,
    status                      TEXT NOT NULL DEFAULT 'approved',
    lang                        VARCHAR(5) NOT NULL,
    canonical_question          TEXT NOT NULL,
    answer                      TEXT NOT NULL,
    tags                        TEXT[] NOT NULL DEFAULT '{}',
    source_ticket_channel_id    BIGINT,
    source_question_message_ids BIGINT[] NOT NULL DEFAULT '{}',
    source_answer_message_ids   BIGINT[] NOT NULL DEFAULT '{}',
    approved_by                 BIGINT,
    approved_at                 TIMESTAMPTZ,
    rejected_by                 BIGINT,
    rejected_reason             TEXT,
    duplicate_of                INTEGER REFERENCES faq_entries(id),
    version                     INTEGER NOT NULL DEFAULT 1,
    content_hash                TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS faq_embeddings (
    faq_id      INTEGER PRIMARY KEY REFERENCES faq_entries(id) ON DELETE CASCADE,
    embedding   FLOAT8[] NOT NULL,
    model       TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS faq_events (
    id           SERIAL PRIMARY KEY,
    faq_id       INTEGER REFERENCES faq_entries(id) ON DELETE SET NULL,
    event_type   TEXT NOT NULL,
    actor_id     BIGINT,
    payload_json JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_base_version (
    name       TEXT PRIMARY KEY,
    version    BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'approved';
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS lang VARCHAR(5) NOT NULL DEFAULT 'en';
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS canonical_question TEXT NOT NULL DEFAULT '';
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS answer TEXT NOT NULL DEFAULT '';
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS source_ticket_channel_id BIGINT;
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS source_question_message_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS source_answer_message_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS approved_by BIGINT;
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS rejected_by BIGINT;
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS rejected_reason TEXT;
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS duplicate_of INTEGER REFERENCES faq_entries(id);
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE faq_entries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE faq_embeddings ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT 'text-embedding-3-large';
ALTER TABLE faq_embeddings ADD COLUMN IF NOT EXISTS dimensions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE faq_embeddings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE faq_events ADD COLUMN IF NOT EXISTS actor_id BIGINT;
ALTER TABLE faq_events ADD COLUMN IF NOT EXISTS payload_json JSONB NOT NULL DEFAULT '{}';

CREATE UNIQUE INDEX IF NOT EXISTS idx_faq_entries_lang_content_hash
    ON faq_entries (lang, content_hash);

CREATE INDEX IF NOT EXISTS idx_faq_entries_status_lang
    ON faq_entries (status, lang);

CREATE INDEX IF NOT EXISTS idx_faq_entries_tags
    ON faq_entries USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_faq_events_faq_id_created_at
    ON faq_events (faq_id, created_at DESC);

CREATE TABLE IF NOT EXISTS support_response_cache (
    cache_key   TEXT PRIMARY KEY,
    docs_version TIMESTAMPTZ,
    response_json JSONB NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_support_response_cache_updated_at
    ON support_response_cache (updated_at DESC);

CREATE TABLE IF NOT EXISTS ticket_knowledge_sync_state (
    channel_id      BIGINT PRIMARY KEY,
    category_id     BIGINT,
    source          TEXT NOT NULL,
    lang            VARCHAR(5),
    last_message_id BIGINT NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    indexed         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ticket_knowledge_sync_state_category
    ON ticket_knowledge_sync_state (category_id);

CREATE INDEX IF NOT EXISTS idx_ticket_knowledge_sync_state_updated_at
    ON ticket_knowledge_sync_state (updated_at DESC);

CREATE TABLE IF NOT EXISTS curseforge_project_state (
    project_id               BIGINT PRIMARY KEY,
    project_slug             TEXT NOT NULL,
    last_processed_file_id   BIGINT,
    last_processed_file_name TEXT,
    last_processed_file_url  TEXT,
    last_processed_at        TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_curseforge_project_state_updated_at
    ON curseforge_project_state (updated_at DESC);

CREATE TABLE IF NOT EXISTS patreon_campaign_state (
    campaign_id              TEXT PRIMARY KEY,
    last_processed_post_id   TEXT,
    last_processed_post_title TEXT,
    last_processed_post_url  TEXT,
    last_processed_at        TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_patreon_campaign_state_updated_at
    ON patreon_campaign_state (updated_at DESC);
