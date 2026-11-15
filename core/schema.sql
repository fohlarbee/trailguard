-- TrailGuard core schema. Safe to run repeatedly.
--
-- Every identifier here (account, bvn, phone, device) is the salted hash produced
-- by a bank's connector. Raw identifiers never reach this database.

CREATE TABLE IF NOT EXISTS accounts (
    account       TEXT PRIMARY KEY,
    bank_id       TEXT NOT NULL,
    bvn           TEXT NOT NULL,
    phone         TEXT NOT NULL,
    device        TEXT NOT NULL,
    kyc_tier      SMALLINT NOT NULL,
    account_type  TEXT NOT NULL,
    opened_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS accounts_bvn ON accounts (bvn);
CREATE INDEX IF NOT EXISTS accounts_device ON accounts (device);

-- A cross-bank transfer is reported by both banks; session_ref is the shared
-- reference, so the second copy is recognised and ignored.
CREATE TABLE IF NOT EXISTS transactions (
    session_ref       TEXT PRIMARY KEY,
    transaction_id    TEXT NOT NULL UNIQUE,
    ts                TIMESTAMPTZ NOT NULL,
    sender_bank       TEXT NOT NULL,
    sender_account    TEXT NOT NULL,
    receiver_bank     TEXT,
    receiver_account  TEXT,
    amount_kobo       BIGINT NOT NULL CHECK (amount_kobo > 0),
    channel           TEXT NOT NULL,
    device            TEXT
);
CREATE INDEX IF NOT EXISTS transactions_sender ON transactions (sender_account, ts);
CREATE INDEX IF NOT EXISTS transactions_receiver ON transactions (receiver_account, ts);

CREATE TABLE IF NOT EXISTS cases (
    case_id         TEXT PRIMARY KEY,
    report_id       TEXT NOT NULL,
    reporting_bank  TEXT NOT NULL,
    reported_at     TIMESTAMPTZ NOT NULL,
    victim_account  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    stolen_kobo     BIGINT,
    traced_as_of    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reporting_bank, report_id)
);

-- The transactions the victim reported as fraudulent.
CREATE TABLE IF NOT EXISTS case_transactions (
    case_id         TEXT NOT NULL REFERENCES cases ON DELETE CASCADE,
    transaction_id  TEXT NOT NULL,
    PRIMARY KEY (case_id, transaction_id)
);

-- The most recent trace of a case: each movement of stolen money, in time order.
CREATE TABLE IF NOT EXISTS trail_hops (
    case_id           TEXT NOT NULL REFERENCES cases ON DELETE CASCADE,
    seq               INT NOT NULL,
    hop               INT NOT NULL,
    session_ref       TEXT NOT NULL,
    transaction_id    TEXT NOT NULL,
    ts                TIMESTAMPTZ NOT NULL,
    sender_bank       TEXT NOT NULL,
    sender_account    TEXT NOT NULL,
    receiver_bank     TEXT,
    receiver_account  TEXT,
    amount_kobo       BIGINT NOT NULL,
    tainted_kobo      BIGINT NOT NULL,
    channel           TEXT NOT NULL,
    endpoint          TEXT,
    PRIMARY KEY (case_id, seq)
);

-- Stolen money still sitting in an account as of the trace: what a hold could catch.
CREATE TABLE IF NOT EXISTS trail_holdings (
    case_id       TEXT NOT NULL REFERENCES cases ON DELETE CASCADE,
    account       TEXT NOT NULL,
    bank_id       TEXT,
    hop           INT NOT NULL,
    tainted_kobo  BIGINT NOT NULL,
    PRIMARY KEY (case_id, account)
);

-- Accounts the mule scorer has flagged, with the moment each first crossed the threshold.
CREATE TABLE IF NOT EXISTS alerts (
    account           TEXT PRIMARY KEY,
    bank_id           TEXT NOT NULL,
    first_flagged_at  TIMESTAMPTZ NOT NULL,
    transaction_id    TEXT NOT NULL,
    score             REAL NOT NULL,
    reasons           JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A hold on money in an account. Holds are temporary: unless a human confirms one,
-- it lapses at expires_at. status: requested (sent to the bank), active (the bank
-- has held money), declined (nothing held), released, expired.
CREATE TABLE IF NOT EXISTS holds (
    hold_id           BIGSERIAL PRIMARY KEY,
    account           TEXT NOT NULL,
    bank_id           TEXT NOT NULL,
    case_id           TEXT,
    requested_kobo    BIGINT NOT NULL CHECK (requested_kobo > 0),
    held_kobo         BIGINT,
    reason            TEXT NOT NULL,
    source            TEXT NOT NULL,          -- auto_flag, agent or analyst
    status            TEXT NOT NULL DEFAULT 'requested',
    placed_at         TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    acknowledged_at   TIMESTAMPTZ,
    reminders         INT NOT NULL DEFAULT 0,
    last_reminded_at  TIMESTAMPTZ,
    review            TEXT,                   -- confirmed or released, set by a human
    reviewed_by       TEXT,
    reviewed_at       TIMESTAMPTZ
);
-- At most one open hold per account.
CREATE UNIQUE INDEX IF NOT EXISTS holds_one_open ON holds (account) WHERE status IN ('requested', 'active');
CREATE INDEX IF NOT EXISTS holds_case ON holds (case_id);
-- The investigator may recommend holding less once a case shows how much is stolen.
-- Only a person at the bank can act on it.
ALTER TABLE holds ADD COLUMN IF NOT EXISTS recommended_kobo BIGINT;
ALTER TABLE holds ADD COLUMN IF NOT EXISTS recommendation TEXT;

CREATE TABLE IF NOT EXISTS evidence_packs (
    pack_id     BIGSERIAL PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases ON DELETE CASCADE,
    built_at    TIMESTAMPTZ NOT NULL,
    hash        TEXT NOT NULL,
    pack        JSONB NOT NULL
);

-- Suspicious transaction report drafts. Only a human can file one.
CREATE TABLE IF NOT EXISTS str_drafts (
    draft_id    BIGSERIAL PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases ON DELETE CASCADE,
    drafted_at  TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',
    body        JSONB NOT NULL,
    filed_by    TEXT,
    filed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id       BIGSERIAL PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES cases ON DELETE CASCADE,
    as_of        TIMESTAMPTZ NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    model        TEXT,
    outcome      TEXT,                        -- completed, refused, stopped or failed
    summary      TEXT,
    steps        JSONB
);

-- Tamper-evident record. Each row's hash covers the previous row's hash and its
-- own body, so changing or removing any earlier row breaks every hash after it.
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL,
    body       TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
