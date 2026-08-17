#!/usr/bin/env bash
#
# First-time bootstrap for a Locus environment. Safe to re-run.
#
# Usage:
#   ADMIN_DATABASE_URL=postgres://... scripts/setup.sh [--skip-db] [--skip-migrations]
#
# What it does:
#   1. Verifies the tools the other scripts assume are installed.
#   2. Creates a local .env from config/env.example if one does not exist,
#      then loads it so values filled into the file are visible to later steps.
#   3. Creates one Postgres role and one database per consuming service.
#   4. Runs pending migrations for every datastore.
#
# Role and database creation lives here rather than in migrations/postgres/
# because it needs credentials and has to connect to the maintenance database
# rather than the database being created. Each service gets its own role and
# database on the shared instance, so no two services can read or write each
# other's tables even though they share a host.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SKIP_DB=false
SKIP_MIGRATIONS=false

# Services that own a Postgres database. The password for each is read from
# <SERVICE>_DB_PASSWORD, upper-cased with dashes turned into underscores.
DB_CONSUMERS=(n8n)

die() {
  echo "error: $*" >&2
  exit 1
}

# Load KEY=VALUE pairs from a dotenv file without overriding variables already
# present in the environment, so `ADMIN_DATABASE_URL=... scripts/setup.sh` still
# wins over a value sitting in .env.
#
# Values are stored literally: $, $$, backticks, and $(...) are not expanded,
# so a password like pa$$word survives. The one exception is a value that is
# exactly $(openssl rand -hex N) — that is the documented generator for
# API_SERVER_KEY, not a general command substitution. The hex is written back
# over the generator so the next run does not rotate the secret.
load_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0
  local line key value nbytes generated
  local generated_keys=() generated_values=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line//[[:space:]]/}" ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" == "$line" ]] && continue
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if eval "[ -n \"\${${key}+x}\" ]"; then
      continue
    fi
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    if [[ "$value" =~ ^\$\(openssl[[:space:]]+rand[[:space:]]+-hex[[:space:]]+([0-9]+)\)$ ]]; then
      nbytes="${BASH_REMATCH[1]}"
      command -v openssl >/dev/null 2>&1 || {
        echo "error: openssl is required to expand ${key}" >&2
        return 1
      }
      generated="$(openssl rand -hex "$nbytes")" || {
        echo "error: openssl rand failed for ${key}" >&2
        return 1
      }
      value="$generated"
      generated_keys+=("$key")
      generated_values+=("$value")
    fi
    # printf -v writes the bytes as a literal. `export name=value` is an
    # assignment word: $, backticks, and $$ in the value are expanded before
    # the variable is set, so a password like pa$$word would not survive.
    printf -v "$key" '%s' "$value"
    # ${key?} is the name to export, not a request to export a variable called key.
    export "${key?}"
  done < "$env_file"

  local i tmp persist_key persist_val persist_line
  i=0
  if [[ ${#generated_keys[@]} -gt 0 ]]; then
    for persist_key in "${generated_keys[@]}"; do
      persist_val="${generated_values[$i]}"
      i=$((i + 1))
      tmp="$(mktemp)"
      while IFS= read -r persist_line || [[ -n "$persist_line" ]]; do
        persist_line="${persist_line%$'\r'}"
        if [[ "$persist_line" == "${persist_key}="* ]]; then
          printf '%s=%s\n' "$persist_key" "$persist_val"
        else
          printf '%s\n' "$persist_line"
        fi
      done < "$env_file" > "$tmp"
      mv "$tmp" "$env_file"
    done
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-db) SKIP_DB=true; shift ;;
    --skip-migrations) SKIP_MIGRATIONS=true; shift ;;
    -h|--help) sed -n '3,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

echo "== Locus setup =="

# --- 1. Tooling -----------------------------------------------------------

missing=()
for tool in psql docker; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  die "missing required tools: ${missing[*]}"
fi

for tool in railway cypher-shell; do
  if command -v "$tool" >/dev/null 2>&1; then
    continue
  fi
  case "$tool" in
    railway)      echo "  note: railway CLI not found — needed for Railway deploys" ;;
    cypher-shell) echo "  note: cypher-shell not found — needed for Neo4j migrations" ;;
  esac
done

# --- 2. Local env file ----------------------------------------------------

if [[ -f .env ]]; then
  echo "-- .env already exists, leaving it alone"
else
  cp config/env.example .env
  echo "-- created .env from config/env.example (fill in real values; it is gitignored)"
fi
load_env_file .env

# --- 3. Per-service roles and databases -----------------------------------

if [[ "$SKIP_DB" == true ]]; then
  echo "-- skipping database bootstrap (--skip-db)"
else
  : "${ADMIN_DATABASE_URL:?set ADMIN_DATABASE_URL to a superuser connection on the Postgres instance}"

  for service in "${DB_CONSUMERS[@]}"; do
    var="$(printf '%s' "$service" | tr '[:lower:]-' '[:upper:]_')_DB_PASSWORD"
    password="${!var:-}"
    [[ -n "$password" ]] || die "set ${var} before bootstrapping the ${service} database"

    echo "-- ${service}: ensuring role and database"

    # Passing the values as psql variables lets psql handle quoting, and
    # format(%I) quotes the identifiers. Neither is interpolated by the shell
    # into SQL text.
    psql "$ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 --quiet \
      -v role="$service" -v db="$service" -v pw="$password" <<'SQL'
-- psql does not interpolate :variables inside dollar-quoted strings, so the
-- values are handed to the DO block as session settings instead.
SET locus.role = :'role';
SET locus.pw   = :'pw';

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = current_setting('locus.role')) THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L',
                       current_setting('locus.role'), current_setting('locus.pw'));
    ELSE
        -- Re-running setup rotates the password to whatever the environment
        -- currently says it is, rather than silently leaving a stale one.
        EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L',
                       current_setting('locus.role'), current_setting('locus.pw'));
    END IF;
END $$;

RESET locus.pw;

SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'role')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')
\gexec

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT ALL PRIVILEGES ON DATABASE :"db" TO :"role";
SQL
  done
fi

# --- 4. Migrations --------------------------------------------------------

if [[ "$SKIP_MIGRATIONS" == true ]]; then
  echo "-- skipping migrations (--skip-migrations)"
else
  echo "-- running Postgres migrations"
  ./scripts/migration.sh --target postgres

  if [[ -n "${NEO4J_URI:-}" ]]; then
    echo "-- running Neo4j migrations"
    ./scripts/migration.sh --target neo4j
  else
    echo "-- NEO4J_URI unset, skipping Neo4j migrations"
  fi
fi

echo "Setup complete."
