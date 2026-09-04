#!/bin/sh
# Hydra bekommt eine eigene Datenbank im selben Postgres. Getrennte Schemata
# wären auch möglich, aber eine eigene Datenbank hält die Dumps sauber trennbar:
# die Blob-Metadaten will man einzeln zurückspielen können, Hydras Tabellen nicht.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
	CREATE DATABASE hydra OWNER $POSTGRES_USER;
EOSQL
