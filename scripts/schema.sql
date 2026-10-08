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


CREATE TABLE IF NOT EXISTS dev_jar_user_downloads (
    discord_user_id BIGINT NOT NULL,
    file_name       TEXT NOT NULL,
    downloaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (discord_user_id, file_name)
);

CREATE INDEX IF NOT EXISTS idx_dev_jar_user_downloads_file_name
    ON dev_jar_user_downloads (file_name);

CREATE TABLE IF NOT EXISTS dev_jar_pending_review (
    id                   INTEGER PRIMARY KEY DEFAULT 1,
    channel_id           BIGINT,
    message_id           BIGINT,
    artifact_file_name   TEXT NOT NULL,
    artifact_version     TEXT NOT NULL,
    artifact_commit_sha  TEXT NOT NULL,
    artifact_sha256      TEXT,
    workflow_run_url     TEXT,
    commits              JSONB NOT NULL DEFAULT '[]',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dev_jar_pending_review_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS dev_jar_published_state (
    id                   INTEGER PRIMARY KEY DEFAULT 1,
    artifact_file_name   TEXT NOT NULL,
    published_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dev_jar_published_state_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS patch_notes_state (
    branch        TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    content_sha   TEXT NOT NULL,
    content       TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (branch, file_path)
);

CREATE TABLE IF NOT EXISTS bug_reports (
    thread_id          BIGINT PRIMARY KEY,
    guild_id           BIGINT,
    reporter_id        BIGINT,
    triage_message_id  BIGINT,
    repo               TEXT,
    issue_number       INTEGER,
    status             TEXT NOT NULL DEFAULT 'triaged',
    ai_title           TEXT,
    ai_summary         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT bug_reports_status_check
        CHECK (status IN ('triaged', 'tracked', 'resolved', 'dismissed'))
);

CREATE INDEX IF NOT EXISTS idx_bug_reports_status
    ON bug_reports (status);

CREATE INDEX IF NOT EXISTS idx_bug_reports_issue
    ON bug_reports (repo, issue_number);

CREATE TABLE IF NOT EXISTS ai_ticket_disabled_channels (
    channel_id   BIGINT PRIMARY KEY,
    disabled_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS member_activity (
    guild_id       BIGINT NOT NULL,
    user_id        BIGINT NOT NULL,
    xp             BIGINT NOT NULL DEFAULT 0,
    level          INTEGER NOT NULL DEFAULT 0,
    last_award_at  TIMESTAMPTZ,
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_member_activity_guild_xp
    ON member_activity (guild_id, xp DESC);

CREATE TABLE IF NOT EXISTS showcase_highlights (
    message_id            BIGINT PRIMARY KEY,
    highlight_message_id  BIGINT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE support_ai_traces ADD COLUMN IF NOT EXISTS confidence REAL;

CREATE TABLE IF NOT EXISTS ticket_image_analyses (
    attachment_id            BIGINT PRIMARY KEY,
    channel_id               BIGINT NOT NULL,
    analysis                 TEXT NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ticket_image_analyses_channel
    ON ticket_image_analyses (channel_id);

CREATE TABLE IF NOT EXISTS ticket_transcripts (
    id                       SERIAL PRIMARY KEY,
    channel_id               BIGINT NOT NULL,
    guild_id                 BIGINT,
    channel_name             TEXT,
    requester_id             BIGINT,
    closed_by_id             BIGINT,
    resolved                 BOOLEAN NOT NULL DEFAULT FALSE,
    ai_confidence            REAL,
    message_count            INTEGER NOT NULL DEFAULT 0,
    title                    TEXT,
    problem                  TEXT,
    resolution               TEXT,
    tags                     TEXT[] NOT NULL DEFAULT '{}',
    knowledge_worthy         BOOLEAN NOT NULL DEFAULT FALSE,
    transcript               TEXT NOT NULL DEFAULT '',
    openai_file_id           TEXT,
    closed_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ticket_transcripts_closed_at
    ON ticket_transcripts (closed_at DESC);

-- Hosted HTML transcript: file <ticket_transcript_dir>/<html_token>.html; NULL expiry with a token = kept forever.
ALTER TABLE ticket_transcripts ADD COLUMN IF NOT EXISTS html_token TEXT;
ALTER TABLE ticket_transcripts ADD COLUMN IF NOT EXISTS html_expires_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_transcripts_html_token
    ON ticket_transcripts (html_token);

CREATE INDEX IF NOT EXISTS idx_ticket_transcripts_channel_id
    ON ticket_transcripts (channel_id);

CREATE TABLE IF NOT EXISTS panel_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    BIGINT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT,
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_panel_audit_log_created_at
    ON panel_audit_log (created_at DESC);

-- Passive bot logs shown in the web panel (services/panel_logs.py); pruned after 365 days.
CREATE TABLE IF NOT EXISTS panel_logs (
    id          BIGSERIAL PRIMARY KEY,
    level       SMALLINT NOT NULL DEFAULT 20,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    user_id     BIGINT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_panel_logs_created_at ON panel_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_panel_logs_source ON panel_logs (source, id DESC);
CREATE INDEX IF NOT EXISTS idx_panel_logs_user ON panel_logs (user_id, id DESC) WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS mod_cases (
    id                BIGSERIAL PRIMARY KEY,
    guild_id          BIGINT NOT NULL,
    user_id           BIGINT NOT NULL,
    moderator_id      BIGINT,
    action            TEXT NOT NULL,
    reason            TEXT,
    duration_seconds  INTEGER,
    source            TEXT NOT NULL DEFAULT 'panel',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mod_cases_user
    ON mod_cases (guild_id, user_id, created_at DESC);

ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS external_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mod_cases_external_id
    ON mod_cases (external_id) WHERE external_id IS NOT NULL;

-- active = FALSE: a deleted warn/note, or a tempban that has been lifted.
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
-- Only tempbans set this; Discord lifts timeouts on its own.
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_mod_cases_expiring
    ON mod_cases (expires_at) WHERE active AND expires_at IS NOT NULL;

-- Components V2 case cards: where the card was posted (so it can edit itself when the case ends),
-- the escalation link, the context line frozen at posting time, and who/what ended the case.
-- Timeouts set expires_at too now, so their card flips to "expired" when Discord lifts them.
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS log_channel_id BIGINT;
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS log_message_id BIGINT;
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS triggered_by BIGINT;
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS context TEXT;
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS ended_by BIGINT;
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
ALTER TABLE mod_cases ADD COLUMN IF NOT EXISTS end_note TEXT;

-- Handled automod alerts collapse to a one-liner; the full details live here for "Show details".
CREATE TABLE IF NOT EXISTS mod_alert_cards (
    message_id  BIGINT PRIMARY KEY,
    details     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Flagged-joiner alerts (raid_guard): recorded the moment the alert is posted, so it's visible in the
-- web panel right away and a bot restart doesn't lose track of the 1h auto-dismiss deadline.
CREATE TABLE IF NOT EXISTS joiner_alerts (
    id                BIGSERIAL PRIMARY KEY,
    guild_id          BIGINT NOT NULL,
    user_id           BIGINT NOT NULL,
    reason            TEXT NOT NULL,
    action_taken      TEXT NOT NULL,
    alert_message_id  BIGINT,
    expires_at        TIMESTAMPTZ NOT NULL,
    outcome           TEXT,
    reviewed_by       BIGINT,
    reviewed_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_joiner_alerts_created
    ON joiner_alerts (guild_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_joiner_alerts_alert
    ON joiner_alerts (alert_message_id) WHERE alert_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_joiner_alerts_due
    ON joiner_alerts (expires_at) WHERE outcome IS NULL;

-- Build gate (cogs/build_gate.py): a Java push asks staff-devs before the dev-jar workflow runs. Rows are
-- the durable state: the 1h auto-close sweep and the live build-progress tracker both resume from here.
CREATE TABLE IF NOT EXISTS build_requests (
    id          BIGSERIAL PRIMARY KEY,
    repo        TEXT NOT NULL,
    branch      TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    pusher      TEXT NOT NULL,
    commits     JSONB NOT NULL DEFAULT '[]'::jsonb,
    source      TEXT NOT NULL DEFAULT 'push',
    status      TEXT NOT NULL DEFAULT 'pending',
    channel_id  BIGINT,
    message_id  BIGINT,
    expires_at  TIMESTAMPTZ NOT NULL,
    decided_by  BIGINT,
    decided_at  TIMESTAMPTZ,
    run_id      BIGINT,
    run_url     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE build_requests ADD COLUMN IF NOT EXISTS changelog TEXT;
ALTER TABLE build_requests ADD COLUMN IF NOT EXISTS preview_message_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_build_requests_created ON build_requests (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_build_requests_open ON build_requests (status) WHERE status IN ('pending', 'building');

-- Channels locked by /lock or a lockdown, with the @everyone overwrites to restore on unlock.
CREATE TABLE IF NOT EXISTS mod_locked_channels (
    channel_id         BIGINT PRIMARY KEY,
    guild_id           BIGINT NOT NULL,
    prev_send          BOOLEAN,
    prev_send_threads  BOOLEAN,
    locked_by          BIGINT,
    locked_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS panel_announcements (
    id            BIGSERIAL PRIMARY KEY,
    author_id     BIGINT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',
    channel_id    BIGINT,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    send_at       TIMESTAMPTZ,
    message_ids   JSONB,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ,
    CONSTRAINT panel_announcements_status_check
        CHECK (status IN ('draft', 'scheduled', 'sending', 'sent', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_panel_announcements_due
    ON panel_announcements (send_at)
    WHERE status = 'scheduled';

CREATE INDEX IF NOT EXISTS idx_panel_announcements_history
    ON panel_announcements (created_at DESC)
    WHERE status NOT IN ('draft', 'scheduled');

CREATE INDEX IF NOT EXISTS idx_panel_announcements_drafts
    ON panel_announcements (updated_at DESC)
    WHERE status = 'draft';

-- One row per automod incident. Staff feedback on the alert (False positive / a punitive click)
-- is the tuning signal: per-filter false-positive rates, threshold suggestions, learned scam images.
CREATE TABLE IF NOT EXISTS automod_hits (
    id                BIGSERIAL PRIMARY KEY,
    guild_id          BIGINT NOT NULL,
    user_id           BIGINT NOT NULL,
    reason            TEXT NOT NULL,
    action            TEXT NOT NULL,
    details           TEXT,
    domains           TEXT[] NOT NULL DEFAULT '{}',
    image_hashes      BIGINT[] NOT NULL DEFAULT '{}',
    scam_hash_id      BIGINT,
    warn_case_id      BIGINT,
    timed_out         BOOLEAN NOT NULL DEFAULT FALSE,
    alert_message_id  BIGINT,
    outcome           TEXT,
    reviewed_by       BIGINT,
    reviewed_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_automod_hits_created
    ON automod_hits (guild_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_automod_hits_alert
    ON automod_hits (alert_message_id) WHERE alert_message_id IS NOT NULL;

-- Known scam images as 64-bit dHashes (stored signed).
CREATE TABLE IF NOT EXISTS scam_image_hashes (
    id           BIGSERIAL PRIMARY KEY,
    hash         BIGINT NOT NULL UNIQUE,
    source       TEXT NOT NULL,
    added_by     BIGINT,
    note         TEXT,
    hits         INTEGER NOT NULL DEFAULT 0,
    last_hit_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- In-house ticket system (cogs/tickets.py). ticket_counter hands out gapless numbers under a row lock.
CREATE TABLE IF NOT EXISTS ticket_counter (
    id           SMALLINT PRIMARY KEY CHECK (id = 1),
    last_number  BIGINT NOT NULL DEFAULT 0
);

INSERT INTO ticket_counter (id, last_number) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id           BIGINT PRIMARY KEY,
    guild_id            BIGINT NOT NULL,
    owner_id            BIGINT NOT NULL,
    channel_id          BIGINT,
    category            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'creating'
                        CHECK (status IN ('creating', 'open', 'closed', 'deleted')),
    channel_name        TEXT,
    language            VARCHAR(5) NOT NULL DEFAULT 'en',
    control_message_id  BIGINT,
    claimed_by          BIGINT,
    closed_by           BIGINT,
    close_reason        TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at           TIMESTAMPTZ,
    deleted_at          TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_channel_id
    ON tickets (channel_id) WHERE channel_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tickets_active_owner
    ON tickets (owner_id) WHERE status IN ('creating', 'open');

-- Every update the self-updater applied or tried: hot reloads, full restarts, failed checks.
CREATE TABLE IF NOT EXISTS bot_updates (
    id           BIGSERIAL PRIMARY KEY,
    sha_from     TEXT NOT NULL,
    sha_to       TEXT NOT NULL,
    mode         TEXT NOT NULL,
    result       TEXT NOT NULL,
    duration_ms  INTEGER,
    detail       TEXT,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bot_updates_applied_at
    ON bot_updates (applied_at DESC);
