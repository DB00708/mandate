-- Mandate schema. The events table is the source of truth (append-only, hash-chained);
-- the other tables are projections written in the same transaction as their events.

CREATE TABLE IF NOT EXISTS households (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    frozen      JSONB,                       -- null or {"by": member, "at": iso}
    last_seq    INTEGER NOT NULL DEFAULT 0,
    head_hash   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    household_id TEXT NOT NULL REFERENCES households(id),
    seq          INTEGER NOT NULL,
    type         TEXT NOT NULL,
    payload      JSONB NOT NULL,
    actor        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,              -- server-assigned (cloud time is authoritative)
    valid_at     TEXT NOT NULL,              -- when it happened in the household (bitemporal)
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL,
    PRIMARY KEY (household_id, seq)
);
-- Append-only: forbid UPDATE/DELETE on the log at the database level.
CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger AS $$
BEGIN
    IF current_setting('mandate.allow_tamper', true) = 'on' THEN
        RETURN NEW;  -- only the red-team tamper demo sets this, inside a test-mode endpoint
    END IF;
    RAISE EXCEPTION 'events are append-only';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS events_no_update ON events;
CREATE TRIGGER events_no_update BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_append_only();

CREATE TABLE IF NOT EXISTS members (
    household_id TEXT NOT NULL REFERENCES households(id),
    member_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('owner', 'adult', 'minor', 'guest')),
    active       BOOLEAN NOT NULL DEFAULT true,
    away_until   DATE,
    voice_profile_ref TEXT,
    PRIMARY KEY (household_id, member_id)
);

CREATE TABLE IF NOT EXISTS devices (
    device_id    TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    member_id    TEXT NOT NULL,
    label        TEXT NOT NULL,
    public_key   TEXT NOT NULL,
    active       BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mandate_versions (
    mandate_id    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    household_id  TEXT NOT NULL REFERENCES households(id),
    doc           JSONB NOT NULL,
    set_by        TEXT NOT NULL,
    valid_from    TIMESTAMPTZ NOT NULL,
    valid_to      TIMESTAMPTZ,
    recorded_from TIMESTAMPTZ NOT NULL,
    recorded_to   TIMESTAMPTZ,
    row_id        BIGSERIAL PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS mv_current ON mandate_versions (household_id) WHERE recorded_to IS NULL;

CREATE TABLE IF NOT EXISTS purchases (
    purchase_id     TEXT PRIMARY KEY,                -- client-generated ULID (dedupes device retries)
    household_id    TEXT NOT NULL REFERENCES households(id),
    requested_by    TEXT,                            -- null = unknown speaker
    speaker_claim   TEXT,
    source          TEXT NOT NULL,                   -- voice | api | mcp | rhythm | import
    utterance       TEXT,
    merchant        TEXT,
    cart            JSONB NOT NULL,                  -- items with unit prices (paise) at request time
    cart_total      INTEGER,                         -- paise incl. fees
    final_total     INTEGER,                         -- merchant checkout total (paise)
    mandate_id      TEXT,
    mandate_version INTEGER,
    state           TEXT NOT NULL,
    uncertain       TEXT,                            -- step whose outcome is UNKNOWN
    decision        JSONB,
    notes           JSONB NOT NULL DEFAULT '[]',
    paypal_order_id TEXT,
    capture_id      TEXT,
    refunded        INTEGER NOT NULL DEFAULT 0,
    approved_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS purchases_hh ON purchases (household_id, created_at);

CREATE TABLE IF NOT EXISTS reservations (
    purchase_id  TEXT PRIMARY KEY REFERENCES purchases(purchase_id),
    household_id TEXT NOT NULL,
    mandate_id   TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    refunded     INTEGER NOT NULL DEFAULT 0,
    month_key    TEXT NOT NULL,
    reserved_at  TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONVERTED', 'RELEASED'))
);

CREATE TABLE IF NOT EXISTS payment_attempts (
    id              BIGSERIAL PRIMARY KEY,
    purchase_id     TEXT NOT NULL,
    step            TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    paypal_order_id TEXT,
    outcome         TEXT NOT NULL,                   -- OK | UNKNOWN | UNAVAILABLE | REJECTED
    detail          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox (
    id              BIGSERIAL PRIMARY KEY,
    household_id    TEXT NOT NULL,
    purchase_id     TEXT NOT NULL,
    step            TEXT NOT NULL,                   -- create | capture | refund
    payload         JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'PENDING', -- PENDING | DONE | CANCELLED | DEAD
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (purchase_id, step)
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (next_attempt_at) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    household_id  TEXT NOT NULL REFERENCES households(id),
    kind          TEXT NOT NULL,                     -- rule_change | escalation | unfreeze
    subject_id    TEXT,                              -- mandate_id or purchase_id
    proposal      JSONB NOT NULL,                    -- new mandate doc / purchase info
    diff          JSONB NOT NULL,
    diff_text     TEXT NOT NULL,
    requested_by  TEXT,
    requested_via TEXT NOT NULL,
    context       JSONB NOT NULL DEFAULT '{}',       -- where/when/speaker score
    challenge     TEXT NOT NULL,
    status        TEXT NOT NULL,                     -- PENDING | APPROVED | REJECTED | EXPIRED | CANCELLED | APPLIED
    decided_by    TEXT,
    device_id     TEXT,
    assertion     JSONB,
    reported      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ NOT NULL,
    effective_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    purchase_id TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
    id           BIGSERIAL PRIMARY KEY,
    household_id TEXT NOT NULL,
    audience     TEXT NOT NULL,                      -- 'speaker' (Alexa speaks) | 'owners' | member id
    kind         TEXT NOT NULL,
    text         TEXT NOT NULL,
    data         JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
