-- The agent's login: SELECT on gold, nothing else. Idempotent; run after gold exists.
-- Its password is set by agent_role.py from Secrets Manager, never written here.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_reader') THEN
        CREATE ROLE agent_reader LOGIN;
    END IF;
END $$;

-- Every session is read-only and short-lived, even if the app forgets to say so.
ALTER ROLE agent_reader NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 10;
ALTER ROLE agent_reader SET default_transaction_read_only = on;
ALTER ROLE agent_reader SET statement_timeout = '15s';
ALTER ROLE agent_reader SET idle_in_transaction_session_timeout = '60s';

-- Nothing by default: no objects in public, no temp tables in this database.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$
BEGIN
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO agent_reader', current_database());
END $$;

-- Gold only. Bronze, silver and meta stay invisible.
REVOKE ALL ON ALL TABLES IN SCHEMA bronze, silver, meta FROM agent_reader;
REVOKE ALL ON SCHEMA bronze, silver, meta FROM agent_reader;
GRANT USAGE ON SCHEMA gold TO agent_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA gold TO agent_reader;
-- Tables the pipeline creates in gold later are readable too.
ALTER DEFAULT PRIVILEGES IN SCHEMA gold GRANT SELECT ON TABLES TO agent_reader;
