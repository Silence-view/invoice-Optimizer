#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PG_BIN="${INVOICE_PG_BIN:-/opt/homebrew/opt/postgresql@15/bin}"
PG_DATA="$PWD/var/postgres"
PG_SOCKET="$PWD/var/pg_socket"
PG_PORT="${INVOICE_PG_PORT:-55432}"
mkdir -p "$PG_SOCKET"
if [ "${1:-start}" = stop ]; then
  "$PG_BIN/pg_ctl" -D "$PG_DATA" stop -m fast
  exit 0
fi
if [ ! -f "$PG_DATA/PG_VERSION" ]; then
  # The cluster password is the one scripts/bootstrap.py generated (DATABASE_URL uses it);
  # .env is read as data, never sourced. There is no shared default password.
  PG_PASSWORD="${INVOICE_DB_PASSWORD:-}"
  if [ -z "$PG_PASSWORD" ] && [ -f .env ]; then
    PG_PASSWORD="$(sed -n 's/^POSTGRES_PASSWORD=//p' .env | head -n 1)"
  fi
  if [ -z "$PG_PASSWORD" ] || [ "$PG_PASSWORD" = REPLACE_WITH_A_RANDOM_SECRET ]; then
    echo 'Run scripts/bootstrap.py (or set INVOICE_DB_PASSWORD) before initialising PostgreSQL.' >&2
    exit 1
  fi
  "$PG_BIN/initdb" -D "$PG_DATA" --username=invoice --auth-local=trust --auth-host=scram-sha-256 --pwfile=<(printf '%s\n' "$PG_PASSWORD") >/dev/null
fi
if ! "$PG_BIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1; then
  "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$PWD/var/postgres.log" -o "-p $PG_PORT -h 127.0.0.1 -k $PG_SOCKET -c shared_buffers=128MB -c max_connections=30" start
fi
if ! "$PG_BIN/psql" -h "$PG_SOCKET" -p "$PG_PORT" -U invoice -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='invoice_optimizer'" | rg -q '^1$'; then
  "$PG_BIN/createdb" -h "$PG_SOCKET" -p "$PG_PORT" -U invoice invoice_optimizer
fi
printf 'Project PostgreSQL ready on 127.0.0.1:%s\n' "$PG_PORT"
