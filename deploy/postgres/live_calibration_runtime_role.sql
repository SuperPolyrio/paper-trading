\set ON_ERROR_STOP on

-- Execute only against the dedicated poly_quant_live_calibration database.
DO $block$
BEGIN
    IF current_database() <> 'poly_quant_live_calibration' THEN
        RAISE EXCEPTION 'refusing live role setup in database %', current_database();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'poly_quant_live_calibration_runtime'
    ) THEN
        CREATE ROLE poly_quant_live_calibration_runtime
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
            NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'poly_quant_live_calibration_app'
    ) THEN
        CREATE ROLE poly_quant_live_calibration_app
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT
            NOREPLICATION NOBYPASSRLS;
    END IF;
END
$block$;

GRANT poly_quant_live_calibration_runtime TO poly_quant_live_calibration_app;
REVOKE CREATE ON SCHEMA public
    FROM poly_quant_live_calibration_runtime, poly_quant_live_calibration_app;
GRANT USAGE ON SCHEMA quant, core, oracle, ops
    TO poly_quant_live_calibration_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA core, oracle, ops
    TO poly_quant_live_calibration_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA quant
    TO poly_quant_live_calibration_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA quant
    TO poly_quant_live_calibration_runtime;

ALTER ROLE poly_quant_live_calibration_app
    SET search_path = quant,core,oracle,ops,public;
GRANT CONNECT ON DATABASE poly_quant_live_calibration
    TO poly_quant_live_calibration_runtime;

ALTER DEFAULT PRIVILEGES IN SCHEMA quant
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES
    TO poly_quant_live_calibration_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA quant
    GRANT USAGE, SELECT ON SEQUENCES
    TO poly_quant_live_calibration_runtime;
