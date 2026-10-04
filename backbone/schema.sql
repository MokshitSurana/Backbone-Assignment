-- Backbone clinical abstraction store.
-- Layer 1 = documents, Layer 2 = claims (what a document asserts),
-- Layer 3 = events (reconciled real-world contacts) + decision log.
-- No field in layer 3 is written without a row in `decisions` explaining why.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS documents (
    doc_pk          TEXT PRIMARY KEY,      -- stable content identity = sha256_norm
    doc_id          TEXT,                  -- publisher id, e.g. BH-D103
    path            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    doc_class       TEXT NOT NULL,         -- see taxonomy.DOC_CLASSES
    authority_class TEXT NOT NULL,
    sha256_raw      TEXT NOT NULL,
    sha256_norm     TEXT NOT NULL,
    char_len        INTEGER NOT NULL,
    received_at     TEXT,                  -- date the org took it into the chart
    authored_at     TEXT,                  -- signature / entry timestamp
    ingested_at     TEXT NOT NULL,
    extractor       TEXT NOT NULL,
    extractor_ver   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_norm ON documents(sha256_norm);

-- One row per duplicate file that resolved to an already-known doc_pk.
CREATE TABLE IF NOT EXISTS document_aliases (
    path    TEXT PRIMARY KEY,
    doc_pk  TEXT NOT NULL REFERENCES documents(doc_pk),
    kind    TEXT NOT NULL,                 -- exact_duplicate | normalized_duplicate
    seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    mrn        TEXT PRIMARY KEY,
    name       TEXT,
    given_name TEXT,
    dob        TEXT
);

-- A claim is one assertion by one document about one encounter/measure/requirement.
-- A document that tabulates several encounters produces several claims.
CREATE TABLE IF NOT EXISTS claims (
    claim_id        TEXT PRIMARY KEY,
    doc_pk          TEXT NOT NULL REFERENCES documents(doc_pk),
    patient_mrn     TEXT,
    claim_type      TEXT NOT NULL,         -- encounter|correction|measure|plan_requirement|episode|observation|charge|authorization
    encounter_ref   TEXT,
    appointment_ref TEXT,
    form_ref        TEXT,
    call_ref        TEXT,
    service_date    TEXT,
    service_raw     TEXT,
    service_type    TEXT,
    presence        TEXT,                  -- present|absent|partial|cancelled|not_patient_contact|unknown
    presence_basis  TEXT,                  -- scheduled|attested|narrative|platform_log|derived
    arrival         TEXT,
    departure       TEXT,
    intervals       TEXT,                  -- JSON [[hh:mm,hh:mm],...] patient-present
    breaks          TEXT,                  -- JSON [[hh:mm,hh:mm],...] nontherapeutic
    stated_minutes  INTEGER,
    minutes_basis   TEXT,                  -- stated_patient|stated_total|computed_from_intervals|scheduled
    authority_class TEXT NOT NULL,
    fields          TEXT,                  -- JSON extras
    quote           TEXT NOT NULL,
    quote_start     INTEGER NOT NULL,
    quote_end       INTEGER NOT NULL,
    quote_verified  INTEGER NOT NULL DEFAULT 0,
    extractor       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_claims_enc  ON claims(patient_mrn, encounter_ref);
CREATE INDEX IF NOT EXISTS ix_claims_date ON claims(patient_mrn, service_date);
CREATE INDEX IF NOT EXISTS ix_claims_doc  ON claims(doc_pk);

-- A correction claim names the encounter and field it replaces.
CREATE TABLE IF NOT EXISTS corrections (
    claim_id     TEXT PRIMARY KEY REFERENCES claims(claim_id),
    target_enc   TEXT,
    target_field TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    patient_mrn       TEXT NOT NULL,
    encounter_ref     TEXT,
    service_date      TEXT NOT NULL,
    service_type      TEXT NOT NULL,
    occurred          TEXT NOT NULL,       -- yes|no|unknown
    patient_present   TEXT NOT NULL,       -- yes|no|partial|unknown
    min_minutes       INTEGER NOT NULL DEFAULT 0,
    max_minutes       INTEGER NOT NULL DEFAULT 0,
    counts_as_therapy INTEGER NOT NULL DEFAULT 0,
    exclusion_reason  TEXT,
    disputed          INTEGER NOT NULL DEFAULT 0,
    resolved          TEXT NOT NULL,       -- JSON resolved field map
    match_rule        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_pat ON events(patient_mrn, service_date);

CREATE TABLE IF NOT EXISTS event_claims (
    event_id   TEXT NOT NULL REFERENCES events(event_id),
    claim_id   TEXT NOT NULL REFERENCES claims(claim_id),
    role       TEXT NOT NULL,              -- primary|corroborating|correction|superseded|rejected
    match_rule TEXT NOT NULL,
    PRIMARY KEY (event_id, claim_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT,
    patient_mrn TEXT,                      -- set even when event_id is NULL, so
                                           -- a decision can be filtered and
                                           -- cleared per patient
    scope       TEXT NOT NULL,             -- event|match|dedupe|plan|measure
    field       TEXT NOT NULL,
    chosen      TEXT,
    rule_id     TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    from_claims TEXT NOT NULL              -- JSON [claim_id,...]
);
CREATE INDEX IF NOT EXISTS ix_dec_event ON decisions(event_id);
CREATE INDEX IF NOT EXISTS ix_dec_pat ON decisions(patient_mrn);

CREATE TABLE IF NOT EXISTS plan_requirements (
    req_id          TEXT PRIMARY KEY,
    patient_mrn     TEXT NOT NULL,
    metric          TEXT NOT NULL,         -- therapy_days|therapy_minutes
    service_types   TEXT NOT NULL,         -- JSON list of canonical types that count
    period          TEXT NOT NULL,         -- week_mon_sun
    comparator      TEXT NOT NULL,
    threshold       REAL NOT NULL,
    effective_start TEXT NOT NULL,
    effective_end   TEXT,
    claim_id        TEXT NOT NULL REFERENCES claims(claim_id)
);

CREATE TABLE IF NOT EXISTS episodes (
    patient_mrn TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT,
    claim_id    TEXT NOT NULL REFERENCES claims(claim_id),
    PRIMARY KEY (patient_mrn, start_date)
);

-- Distinct symptom assessments. Re-imports collapse onto the same identity.
CREATE TABLE IF NOT EXISTS measures (
    measure_id   TEXT PRIMARY KEY,         -- mrn|instrument|form_ref|completed_date
    patient_mrn  TEXT NOT NULL,
    instrument   TEXT NOT NULL,
    form_ref     TEXT,
    completed_at TEXT NOT NULL,
    total        REAL,
    items        TEXT,
    claim_ids    TEXT NOT NULL,            -- JSON list
    disputed     INTEGER NOT NULL DEFAULT 0,
    total_conflicts TEXT                   -- JSON [{total, claim_id}] when two
                                           -- records of one administration
                                           -- disagree about the score
);

-- Narrative progress evidence, kept with its exact source span.
CREATE TABLE IF NOT EXISTS observations (
    obs_id      TEXT PRIMARY KEY,
    patient_mrn TEXT NOT NULL,
    obs_date    TEXT NOT NULL,
    domain      TEXT NOT NULL,
    polarity    TEXT NOT NULL,             -- improvement|persistence|worsening|neutral
    reporter    TEXT NOT NULL,             -- patient|clinician|informant
    claim_id    TEXT NOT NULL REFERENCES claims(claim_id),
    quote       TEXT NOT NULL,
    quote_start INTEGER NOT NULL,
    quote_end   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_obs_pat ON observations(patient_mrn, obs_date);

CREATE TABLE IF NOT EXISTS run_log (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    command    TEXT NOT NULL,
    detail     TEXT
);
