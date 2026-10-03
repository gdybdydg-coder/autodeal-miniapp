-- Proposed future PostgreSQL migration; NOT executed against production.
-- New telemetry only. No inferred backfill or changes to source_budgets.
BEGIN;
CREATE TABLE ria_api_attempts (
    id VARCHAR(32) PRIMARY KEY,
    request_fingerprint VARCHAR(64) NOT NULL,
    category VARCHAR(24) NOT NULL,
    reserved_at DOUBLE PRECISION NOT NULL,
    dispatched_at DOUBLE PRECISION,
    transport_started_at DOUBLE PRECISION,
    finished_at DOUBLE PRECISION,
    state VARCHAR(24) NOT NULL,
    error_code VARCHAR(40),
    http_status INTEGER,
    call_relation VARCHAR(32) NOT NULL,
    request_ordinal INTEGER NOT NULL,
    prior_attempt_id VARCHAR(32),
    forced BOOLEAN NOT NULL
);
CREATE INDEX ria_attempt_window_category ON ria_api_attempts (reserved_at, category);
CREATE INDEX ria_attempt_transport_category ON ria_api_attempts (transport_started_at, category);
CREATE INDEX ria_attempt_fingerprint_time ON ria_api_attempts (request_fingerprint, reserved_at);
COMMIT;
