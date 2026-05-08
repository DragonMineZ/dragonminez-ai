-- ============================================================
-- DragonMineZ bot operational schema (idempotent)
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

CREATE TABLE IF NOT EXISTS faq_events (
    id           SERIAL PRIMARY KEY,
    faq_id       INTEGER REFERENCES faq_entries(id) ON DELETE SET NULL,
    event_type   TEXT NOT NULL,
    actor_id     BIGINT,
    payload_json JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS faq_review_candidates (
    id                          SERIAL PRIMARY KEY,
    status                      TEXT NOT NULL DEFAULT 'pending',
    lang                        VARCHAR(5) NOT NULL DEFAULT 'en',
    canonical_question          TEXT NOT NULL,
    answer                      TEXT NOT NULL,
    tags                        TEXT[] NOT NULL DEFAULT '{}',
    source_ticket_channel_id    BIGINT,
    source_question_message_ids BIGINT[] NOT NULL DEFAULT '{}',
    source_answer_message_ids   BIGINT[] NOT NULL DEFAULT '{}',
    proposed_by                 BIGINT,
    reviewed_by                 BIGINT,
    review_reason               TEXT,
    approved_faq_id             INTEGER REFERENCES faq_entries(id) ON DELETE SET NULL,
    review_channel_id           BIGINT,
    review_message_id           BIGINT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
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

ALTER TABLE faq_events ADD COLUMN IF NOT EXISTS actor_id BIGINT;
ALTER TABLE faq_events ADD COLUMN IF NOT EXISTS payload_json JSONB NOT NULL DEFAULT '{}';

ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS lang VARCHAR(5) NOT NULL DEFAULT 'en';
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS canonical_question TEXT NOT NULL DEFAULT '';
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS answer TEXT NOT NULL DEFAULT '';
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS source_ticket_channel_id BIGINT;
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS source_question_message_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS source_answer_message_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS proposed_by BIGINT;
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS reviewed_by BIGINT;
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS review_reason TEXT;
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS approved_faq_id INTEGER REFERENCES faq_entries(id) ON DELETE SET NULL;
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS review_channel_id BIGINT;
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS review_message_id BIGINT;
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE faq_review_candidates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS idx_faq_entries_lang_content_hash
    ON faq_entries (lang, content_hash);

CREATE INDEX IF NOT EXISTS idx_faq_entries_status_lang
    ON faq_entries (status, lang);

CREATE INDEX IF NOT EXISTS idx_faq_entries_tags
    ON faq_entries USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_faq_events_faq_id_created_at
    ON faq_events (faq_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_faq_review_candidates_status_created_at
    ON faq_review_candidates (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_faq_review_candidates_review_message
    ON faq_review_candidates (review_channel_id, review_message_id);

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
