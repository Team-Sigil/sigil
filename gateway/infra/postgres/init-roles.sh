#!/bin/sh
# Creates the DB roles from SPEC §14 on first start. Grants on tables are applied by migrations.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_pw="$SIGIL_APP_PASSWORD" -v ro_pw="$SIGIL_SIGNER_RO_PASSWORD" <<'SQL'
CREATE ROLE sigil_app LOGIN PASSWORD :'app_pw';
CREATE ROLE sigil_signer_ro LOGIN PASSWORD :'ro_pw';
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sigil_app, sigil_signer_ro;
CREATE DATABASE sigil_test OWNER sigil_owner;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname sigil_test <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sigil_app, sigil_signer_ro;
SQL
