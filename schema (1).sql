-- ============================================================
--  RG Pick-A-Part  –  Supabase Schema
--  Run once in Supabase SQL Editor
-- ============================================================

-- ── Vehicles (one row per unique stock number) ────────────────────────────
CREATE TABLE IF NOT EXISTS vehicles (
    stock               INTEGER PRIMARY KEY,
    year                SMALLINT,
    make                TEXT,
    model               TEXT,
    trim                TEXT,
    row                 SMALLINT,
    lot                 TEXT,
    raw_text            TEXT,

    -- Date tracking
    first_seen_date     DATE NOT NULL,          -- day scraper first saw it
    last_seen_date      DATE,                   -- day scraper last confirmed it
    estimated_add_date  DATE,                   -- interpolated from anchors
    confirmed_add_date  DATE,                   -- manually confirmed override
    removed_date        DATE,                   -- day it disappeared

    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ── Manually confirmed stock → date anchor points ─────────────────────────
CREATE TABLE IF NOT EXISTS stock_anchors (
    stock           INTEGER PRIMARY KEY,
    confirmed_date  DATE NOT NULL,
    note            TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Daily snapshot (one row per scrape run) ───────────────────────────────
CREATE TABLE IF NOT EXISTS daily_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_date   DATE NOT NULL UNIQUE,
    total_active    INTEGER,
    added_count     INTEGER,
    removed_count   INTEGER,
    added_stocks    INTEGER[],
    removed_stocks  INTEGER[],
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Vehicle flags & checklist (one row per flagged stock) ────────────────────
-- If this table already exists without checklist_status, only the ALTER TABLE
-- line below needs to run — it is safe to run on an existing table.
CREATE TABLE IF NOT EXISTS vehicle_flags (
    stock               INTEGER PRIMARY KEY REFERENCES vehicles(stock) ON DELETE CASCADE,
    flag                TEXT NOT NULL,          -- 'interested' | 'worked' | 'incorrect'
    note                TEXT DEFAULT '',        -- free-text note shown on checklist
    checklist_status    TEXT,                   -- 'complete' | 'broken' | 'not_there' | 'couldnt_remove'
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Migration: run this if vehicle_flags already exists but is missing checklist_status
ALTER TABLE vehicle_flags ADD COLUMN IF NOT EXISTS checklist_status TEXT;

CREATE INDEX IF NOT EXISTS idx_vehicle_flags_flag ON vehicle_flags (flag);

-- ── Indexes ───────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_vehicles_active       ON vehicles (is_active);
CREATE INDEX IF NOT EXISTS idx_vehicles_first_seen   ON vehicles (first_seen_date DESC);
CREATE INDEX IF NOT EXISTS idx_vehicles_est_date     ON vehicles (estimated_add_date DESC);
CREATE INDEX IF NOT EXISTS idx_vehicles_make_model   ON vehicles (make, model);
CREATE INDEX IF NOT EXISTS idx_vehicles_removed      ON vehicles (removed_date DESC) WHERE removed_date IS NOT NULL;

-- ── Trigger: auto-update updated_at ──────────────────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER vehicles_updated_at
    BEFORE UPDATE ON vehicles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ── Handy views ───────────────────────────────────────────────────────────

-- Latest active inventory
CREATE OR REPLACE VIEW v_active AS
SELECT
    stock,
    year,
    make,
    model,
    trim,
    row,
    lot,
    COALESCE(confirmed_add_date, estimated_add_date, first_seen_date) AS best_add_date,
    first_seen_date,
    estimated_add_date,
    confirmed_add_date
FROM vehicles
WHERE is_active = TRUE
ORDER BY stock DESC;

-- Added in last N days (parameterize in app)
CREATE OR REPLACE VIEW v_recent_14 AS
SELECT * FROM v_active
WHERE first_seen_date >= CURRENT_DATE - INTERVAL '14 days';

CREATE OR REPLACE VIEW v_recent_7 AS
SELECT * FROM v_active
WHERE first_seen_date >= CURRENT_DATE - INTERVAL '7 days';

CREATE OR REPLACE VIEW v_recent_3 AS
SELECT * FROM v_active
WHERE first_seen_date >= CURRENT_DATE - INTERVAL '3 days';

CREATE OR REPLACE VIEW v_today AS
SELECT * FROM v_active
WHERE first_seen_date = CURRENT_DATE;

-- Removed in last 7 days
CREATE OR REPLACE VIEW v_removed_7 AS
SELECT
    stock, year, make, model, trim, row, lot, removed_date,
    first_seen_date,
    (removed_date - first_seen_date) AS days_on_lot
FROM vehicles
WHERE is_active = FALSE
  AND removed_date >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY removed_date DESC;
