\set ON_ERROR_STOP on

-- Capability role: paper execution may mutate its isolated execution schema but
-- cannot create roles, databases, schemas, or bypass row-level security.
DO $block$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'poly_quant_paper_runtime') THEN
        CREATE ROLE poly_quant_paper_runtime
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'poly_quant_paper_app') THEN
        CREATE ROLE poly_quant_paper_app
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END
$block$;

GRANT poly_quant_paper_runtime TO poly_quant_paper_app;
REVOKE CREATE ON SCHEMA public FROM poly_quant_paper_runtime, poly_quant_paper_app;
GRANT USAGE ON SCHEMA quant, core, oracle, ops TO poly_quant_paper_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA core, oracle, ops TO poly_quant_paper_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA quant TO poly_quant_paper_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA quant TO poly_quant_paper_runtime;

ALTER ROLE poly_quant_paper_app SET search_path = quant,core,oracle,ops,public;

DO $block$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO poly_quant_paper_runtime',
        current_database()
    );
END
$block$;

-- Run as the owner that creates future paper tables.
ALTER DEFAULT PRIVILEGES IN SCHEMA quant
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO poly_quant_paper_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA quant
    GRANT USAGE, SELECT ON SEQUENCES TO poly_quant_paper_runtime;

COMMENT ON ROLE poly_quant_paper_runtime IS
    'Paper-only execution capability; never grant live calibration membership.';
COMMENT ON ROLE poly_quant_paper_app IS
    'Paper-only login; password is rotated outside SQL through Secret Manager.';
