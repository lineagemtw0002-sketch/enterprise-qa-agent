DO
$$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'eqa_readonly') THEN
      CREATE ROLE eqa_readonly LOGIN PASSWORD 'eqa_readonly';
   END IF;
END
$$;

GRANT CONNECT ON DATABASE eqa TO eqa_readonly;
GRANT USAGE ON SCHEMA public TO eqa_readonly;

-- Table-level SELECT grants for the whitelisted business tables are applied by the
-- app at startup (see backend/db/session.py:grant_readonly_access), once those
-- tables exist. We deliberately do NOT grant blanket access to future tables here,
-- so new tables are private-by-default until explicitly whitelisted.
