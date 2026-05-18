-- ============================================================
-- DragonMineZ bot operational schema (idempotent)
-- ============================================================

CREATE TABLE IF NOT EXISTS support_sessions (
    channel_id                 BIGINT PRIMARY KEY,
    openai_conversation_id     TEXT NOT NULL,
    last_response_id           TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE support_sessions ADD COLUMN IF NOT EXISTS openai_conversation_id TEXT NOT NULL DEFAULT '';
ALTER TABLE support_sessions ADD COLUMN IF NOT EXISTS last_response_id TEXT;
ALTER TABLE support_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE support_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_support_sessions_updated_at
    ON support_sessions (updated_at DESC);

CREATE TABLE IF NOT EXISTS support_ai_traces (
    id                       SERIAL PRIMARY KEY,
    workflow                 TEXT NOT NULL,
    response_id              TEXT,
    openai_conversation_id   TEXT,
    previous_response_id     TEXT,
    model                    TEXT NOT NULL,
    language                 VARCHAR(5),
    channel_id               BIGINT,
    user_id                  BIGINT,
    prompt_cache_key         TEXT,
    file_search_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
    vector_store_ids         TEXT[] NOT NULL DEFAULT '{}',
    tool_names               TEXT[] NOT NULL DEFAULT '{}',
    latency_ms               INTEGER,
    input_tokens             INTEGER,
    output_tokens            INTEGER,
    total_tokens             INTEGER,
    cached_tokens            INTEGER,
    reasoning_tokens         INTEGER,
    reply_text               TEXT,
    input_json               JSONB NOT NULL DEFAULT '[]',
    request_metadata         JSONB NOT NULL DEFAULT '{}',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS workflow TEXT NOT NULL DEFAULT 'support_question';
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS response_id TEXT;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS openai_conversation_id TEXT;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS previous_response_id TEXT;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS language VARCHAR(5);
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS channel_id BIGINT;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS prompt_cache_key TEXT;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS file_search_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS vector_store_ids TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS tool_names TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS latency_ms INTEGER;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS input_tokens INTEGER;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS output_tokens INTEGER;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS total_tokens INTEGER;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS cached_tokens INTEGER;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS reply_text TEXT;
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS input_json JSONB NOT NULL DEFAULT '[]';
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS request_metadata JSONB NOT NULL DEFAULT '{}';
ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_support_ai_traces_created_at
    ON support_ai_traces (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_support_ai_traces_response_id
    ON support_ai_traces (response_id);

CREATE INDEX IF NOT EXISTS idx_support_ai_traces_channel_created_at
    ON support_ai_traces (channel_id, created_at DESC);

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

CREATE TABLE IF NOT EXISTS patreon_links (
    discord_user_id          BIGINT PRIMARY KEY,
    discord_username         TEXT NOT NULL,
    patreon_user_id          TEXT NOT NULL,
    patreon_member_id        TEXT,
    patreon_full_name        TEXT,
    patron_status            TEXT,
    tier_ids                 TEXT[] NOT NULL DEFAULT '{}',
    last_charge_date         TIMESTAMPTZ,
    entitlement_active       BOOLEAN NOT NULL DEFAULT FALSE,
    linked_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS discord_username TEXT NOT NULL DEFAULT '';
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS patreon_user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS patreon_member_id TEXT;
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS patreon_full_name TEXT;
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS patron_status TEXT;
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS tier_ids TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS last_charge_date TIMESTAMPTZ;
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS entitlement_active BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS linked_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE patreon_links ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS idx_patreon_links_patreon_user_id
    ON patreon_links (patreon_user_id);

CREATE INDEX IF NOT EXISTS idx_patreon_links_patreon_member_id
    ON patreon_links (patreon_member_id);

CREATE INDEX IF NOT EXISTS idx_patreon_links_entitlement_active
    ON patreon_links (entitlement_active);

CREATE TABLE IF NOT EXISTS patreon_whitelist_grants (
    id                          BIGSERIAL PRIMARY KEY,
    owner_discord_user_id       BIGINT NOT NULL,
    beneficiary_discord_user_id BIGINT NOT NULL,
    beneficiary_discord_username TEXT NOT NULL,
    minecraft_username          TEXT NOT NULL,
    kind                        TEXT NOT NULL,
    active                      BOOLEAN NOT NULL DEFAULT TRUE,
    source_pr_url               TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT patreon_whitelist_grants_kind_check
        CHECK (kind IN ('self', 'gift'))
);

ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS owner_discord_user_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS beneficiary_discord_user_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS beneficiary_discord_username TEXT NOT NULL DEFAULT '';
ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS minecraft_username TEXT NOT NULL DEFAULT '';
ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'self';
ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS source_pr_url TEXT;
ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE patreon_whitelist_grants ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS idx_patreon_whitelist_grants_identity
    ON patreon_whitelist_grants (owner_discord_user_id, beneficiary_discord_user_id, kind);

CREATE INDEX IF NOT EXISTS idx_patreon_whitelist_grants_owner_active
    ON patreon_whitelist_grants (owner_discord_user_id, active);

CREATE INDEX IF NOT EXISTS idx_patreon_whitelist_grants_minecraft_username
    ON patreon_whitelist_grants (minecraft_username);
