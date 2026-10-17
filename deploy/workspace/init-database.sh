#!/bin/sh
set -eu
# Runs once on an empty volume, as the separate PostgreSQL bootstrap account.
# psql quotes the password as a literal; it is never passed in command arguments.
psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv application_password MERIDIAN_DATABASE_PASSWORD
CREATE ROLE meridian LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'application_password';
CREATE DATABASE meridian OWNER meridian;
REVOKE ALL ON DATABASE meridian FROM PUBLIC;
\connect meridian
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO meridian;
SQL
