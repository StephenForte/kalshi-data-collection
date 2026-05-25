-- =============================================================================
-- Kalshi Monitor — SQLite schema v2
-- =============================================================================
-- Changes from v1:
--   * New `contracts` table — child of contract_series, models Kalshi's
--     event→market hierarchy. One row per Kalshi sub-market (i.e., one row
--     per strike for cumulative markets, one row per outcome for ME markets).
--   * contract_details now FKs to contracts (not contract_series).
--   * strike moved from contract_details to contracts — it's a property of
--     the sub-market, not the snapshot.
--   * notable_event column dropped from dashboard_summary (all-null in source).
--   * Views updated to maintain the Airtable-shape interface.
--
-- ME-market historical data note:
--   For FF_Rate and Recession, Airtable rows lack a sub-market discriminator,
--   and we verified positions are random (stdev ≈ range/2 across positions).
--   Historical ME rows therefore collapse into one fake "bucket" contract per
--   event, code prefixed F- so they're easy to filter or remove later.
--   New data fetched via kalshi_fetch_sqlite.py will write real sub-market
--   tickers and won't carry the F- prefix.
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- markets — lookup table for the six tracked market types
-- -----------------------------------------------------------------------------
CREATE TABLE markets (
    id     INTEGER PRIMARY KEY,
    code   TEXT NOT NULL UNIQUE
           CHECK (code IN ('CPI', 'Core_CPI_MoM', 'GDP', 'Recession', 'FF_Rate', 'Payrolls')),
    label  TEXT NOT NULL
);

INSERT INTO markets (code, label) VALUES
    ('CPI',          'CPI (YoY)'),
    ('Core_CPI_MoM', 'Core CPI (MoM)'),
    ('GDP',          'GDP Growth'),
    ('Recession',    'Recession Probability'),
    ('FF_Rate',      'Fed Funds Rate'),
    ('Payrolls',     'Payrolls');


-- -----------------------------------------------------------------------------
-- contract_series — Kalshi event (e.g., KXFEDDECISION-26JUN)
-- -----------------------------------------------------------------------------
CREATE TABLE contract_series (
    id                INTEGER PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,
    market_id         INTEGER NOT NULL,
    strike_structure  TEXT
                      CHECK (strike_structure IN ('cumulative', 'mutually_exclusive') OR strike_structure IS NULL),
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX idx_contract_series_market ON contract_series(market_id);


-- -----------------------------------------------------------------------------
-- contracts — Kalshi sub-market (a specific strike or outcome under an event)
-- -----------------------------------------------------------------------------
-- For cumulative markets: one row per distinct strike under the event.
-- For ME markets historical: one F-prefixed "bucket" row per event.
-- For ME markets going forward: one row per Kalshi sub-market ticker.
--
-- `strike` is here because it's a property of the contract (sub-market),
-- not of the snapshot. Cumulative contracts have a strike; ME contracts don't.
CREATE TABLE contracts (
    id                    INTEGER PRIMARY KEY,
    contract_series_id    INTEGER NOT NULL,
    code                  TEXT NOT NULL UNIQUE,    -- Kalshi sub-market ticker, or F-prefixed bucket
    strike                REAL,                     -- NULL for ME contracts
    label                 TEXT,                     -- e.g. "25bp cut", "≥ 2.5%" — NULL until populated
    is_synthetic          INTEGER NOT NULL DEFAULT 0
                          CHECK (is_synthetic IN (0, 1)),
    FOREIGN KEY (contract_series_id) REFERENCES contract_series(id)
);

CREATE INDEX idx_contracts_series ON contracts(contract_series_id);
CREATE INDEX idx_contracts_synthetic ON contracts(is_synthetic);


-- -----------------------------------------------------------------------------
-- contract_details — one row per contract per snapshot
-- -----------------------------------------------------------------------------
-- For cumulative markets, this is one row per (strike, timestamp) — same as
-- before, just keyed via contracts instead of (series + strike).
-- For ME markets historical: many rows per timestamp all pointing at the
-- same synthetic bucket contract (since we can't tell them apart).
-- For ME markets going forward: one row per (sub-market, timestamp).
CREATE TABLE contract_details (
    id                    INTEGER PRIMARY KEY,
    contract_id           INTEGER NOT NULL,
    timestamp             TEXT NOT NULL,
    yes_price             REAL,
    implied_probability   REAL,
    volume                INTEGER,
    days_to_event         INTEGER,     -- denormalized
    market_volume_usd     REAL,
    FOREIGN KEY (contract_id) REFERENCES contracts(id)
);

-- Natural-key uniqueness is per (contract, timestamp). For synthetic ME bucket
-- contracts, this would collapse all snapshot rows into one — which is wrong;
-- we want to preserve the historical N-rows-per-snapshot data. So no UNIQUE
-- constraint on (contract_id, timestamp); just an index for query speed.
-- Real (non-synthetic) contracts can still upsert by adding a separate path
-- in the loader if needed; for now we rely on the application to not
-- double-write.
CREATE INDEX idx_contract_details_contract_ts
    ON contract_details(contract_id, timestamp);

CREATE INDEX idx_contract_details_timestamp
    ON contract_details(timestamp);


-- -----------------------------------------------------------------------------
-- dashboard_summary — event-level aggregate (one row per event per snapshot)
-- -----------------------------------------------------------------------------
-- Unchanged from v1 except `notable_event` is dropped.
CREATE TABLE dashboard_summary (
    id                    INTEGER PRIMARY KEY,
    contract_series_id    INTEGER NOT NULL,
    timestamp             TEXT NOT NULL,
    implied_mean          REAL,
    days_to_event         INTEGER,
    market_volume_usd     REAL,
    std_dev               REAL,
    skewness              REAL,
    FOREIGN KEY (contract_series_id) REFERENCES contract_series(id)
);

CREATE UNIQUE INDEX idx_dashboard_summary_natural
    ON dashboard_summary(contract_series_id, timestamp);

CREATE INDEX idx_dashboard_summary_timestamp
    ON dashboard_summary(timestamp);


-- -----------------------------------------------------------------------------
-- accuracy_log — one row per scored release
-- -----------------------------------------------------------------------------
CREATE TABLE accuracy_log (
    id                    INTEGER PRIMARY KEY,
    market_id             INTEGER NOT NULL,
    contract_series_id    INTEGER NOT NULL,
    run_date              TEXT NOT NULL,
    release_date          TEXT NOT NULL,
    kalshi_implied_mean   REAL,
    actual_value          REAL,
    error                 REAL,
    abs_error             REAL,
    days_before           INTEGER,
    readings_in_window    INTEGER,
    FOREIGN KEY (market_id) REFERENCES markets(id),
    FOREIGN KEY (contract_series_id) REFERENCES contract_series(id)
);

CREATE UNIQUE INDEX idx_accuracy_log_natural
    ON accuracy_log(market_id, release_date);

CREATE INDEX idx_accuracy_log_release_date
    ON accuracy_log(release_date);


-- -----------------------------------------------------------------------------
-- event_annotations
-- -----------------------------------------------------------------------------
CREATE TABLE event_annotations (
    id            INTEGER PRIMARY KEY,
    market_id     INTEGER NOT NULL,
    event_date    TEXT NOT NULL,
    label         TEXT NOT NULL,
    type          TEXT NOT NULL
                  CHECK (type IN ('fomc', 'cpi', 'core_cpi', 'gdp', 'payrolls', 'other')),
    notes         TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE UNIQUE INDEX idx_event_annotations_natural
    ON event_annotations(market_id, event_date, label);

CREATE INDEX idx_event_annotations_date
    ON event_annotations(event_date);


-- =============================================================================
-- Views — reproduce the Airtable-shape interface for code that doesn't yet
-- know about the contracts table.
-- =============================================================================

CREATE VIEW v_dashboard_summary AS
SELECT
    ds.id,
    ds.timestamp,
    m.code              AS market_type,
    cs.code             AS contract_series,
    ds.implied_mean,
    ds.days_to_event,
    cs.strike_structure,
    ds.market_volume_usd,
    ds.std_dev,
    ds.skewness
FROM dashboard_summary ds
JOIN contract_series cs ON cs.id = ds.contract_series_id
JOIN markets         m  ON m.id  = cs.market_id;

CREATE VIEW v_accuracy_log AS
SELECT
    al.id,
    al.run_date,
    m.code              AS market_type,
    cs.code             AS contract_series,
    al.release_date,
    al.kalshi_implied_mean,
    al.actual_value,
    al.error,
    al.abs_error,
    al.days_before,
    al.readings_in_window
FROM accuracy_log al
JOIN markets         m  ON m.id  = al.market_id
JOIN contract_series cs ON cs.id = al.contract_series_id;

CREATE VIEW v_event_annotations AS
SELECT
    ea.id,
    ea.label,
    ea.type,
    ea.event_date,
    m.code              AS market_type,
    ea.notes
FROM event_annotations ea
JOIN markets m ON m.id = ea.market_id;

-- contract_details view: joins all the way back up so callers see the same
-- shape Airtable did (market_type + contract_series + strike on every row)
CREATE VIEW v_contract_details AS
SELECT
    cd.id,
    cd.timestamp,
    m.code              AS market_type,
    cs.code             AS contract_series,
    c.code              AS contract_code,
    c.strike,
    c.label             AS contract_label,
    c.is_synthetic,
    cd.yes_price,
    cd.implied_probability,
    cd.volume,
    cd.days_to_event,
    cs.strike_structure,
    cd.market_volume_usd
FROM contract_details cd
JOIN contracts       c  ON c.id  = cd.contract_id
JOIN contract_series cs ON cs.id = c.contract_series_id
JOIN markets         m  ON m.id  = cs.market_id;

-- "Clean" view: excludes synthetic contracts. Use this when you want analysis
-- only on properly-modeled data.
CREATE VIEW v_contract_details_clean AS
SELECT * FROM v_contract_details WHERE is_synthetic = 0;

-- Inventory view: contracts with their snapshot counts. Handy during dev.
CREATE VIEW v_contracts_summary AS
SELECT
    m.code              AS market_type,
    cs.code             AS contract_series,
    c.code              AS contract_code,
    c.strike,
    c.is_synthetic,
    COUNT(cd.id)        AS snapshot_count,
    MIN(cd.timestamp)   AS first_seen,
    MAX(cd.timestamp)   AS last_seen
FROM contracts c
JOIN contract_series cs ON cs.id = c.contract_series_id
JOIN markets         m  ON m.id  = cs.market_id
LEFT JOIN contract_details cd ON cd.contract_id = c.id
GROUP BY c.id;
