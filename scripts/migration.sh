#!/usr/bin/env bash
#
# Applies pending migrations to a target datastore and records what it applied.
#
# Usage:
#   scripts/migration.sh --target postgres [--file <path>] [--dry-run]
#   scripts/migration.sh --target neo4j    [--file <path>] [--dry-run]
#
# With no --file, every migration under migrations/<target>/ is applied in
# filename order. Already-applied migrations are skipped, so a full run is
# safe to repeat. A migration whose contents changed after it was applied is
# a hard error rather than a re-run: edit-after-apply means the live schema
# and the repository have diverged, and replaying the new version would not
# reconcile them.
#
# Postgres migrations run inside a single transaction together with their
# bookkeeping row, so a failure leaves nothing recorded. Statements that
# cannot run inside a transaction (CREATE DATABASE, CREATE INDEX
# CONCURRENTLY, ALTER TYPE ... ADD VALUE) must say so on their own line:
#
#   -- migration:no-transaction
#
# Those are recorded after the fact and are therefore not atomic: if one
# fails halfway, inspect the database before re-running.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET=""
SINGLE_FILE=""
DRY_RUN=false

die() {
  echo "error: $*" >&2
  exit 1
}

usage() {
  sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --file)   SINGLE_FILE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ -n "$TARGET" ]] || die "--target is required (postgres|neo4j)"
case "$TARGET" in
  postgres|neo4j) ;;
  *) die "unsupported target: $TARGET (expected postgres|neo4j)" ;;
esac

MIGRATION_DIR="${REPO_ROOT}/migrations/${TARGET}"

# macOS ships shasum, most Linux images ship sha256sum. Fail loudly rather
# than silently skipping drift detection.
checksum() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    die "no sha256 tool found (need sha256sum or shasum)"
  fi
}

# Collect the migrations to consider. LC_ALL=C keeps ordering identical
# across machines and locales.
collect_migrations() {
  local ext="$1"
  if [[ -n "$SINGLE_FILE" ]]; then
    local path="$SINGLE_FILE"
    [[ -f "$path" ]] || path="${REPO_ROOT}/${SINGLE_FILE}"
    [[ -f "$path" ]] || die "no such migration file: $SINGLE_FILE"
    printf '%s\n' "$path"
    return
  fi
  [[ -d "$MIGRATION_DIR" ]] || die "no migration directory: $MIGRATION_DIR"
  find "$MIGRATION_DIR" -maxdepth 1 -name "*.${ext}" -type f | LC_ALL=C sort
}

# ---------------------------------------------------------------- postgres

postgres_setup() {
  command -v psql >/dev/null 2>&1 || die "psql not found on PATH"
  : "${DATABASE_URL:?set DATABASE_URL to the target database}"

  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --quiet <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text        PRIMARY KEY,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
SQL
}

postgres_applied_checksum() {
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --quiet --no-align --tuples-only \
    -c "SELECT checksum FROM schema_migrations WHERE filename = '$1'"
}

postgres_apply() {
  local path="$1" name="$2" sum="$3"
  local record="INSERT INTO schema_migrations (filename, checksum) VALUES ('${name}', '${sum}');"

  if grep -qE '^[[:space:]]*--[[:space:]]*migration:no-transaction' "$path"; then
    echo "   (no-transaction migration: not atomic)"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --quiet -f "$path"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --quiet -c "$record"
  else
    # Migration and its bookkeeping row commit or roll back together.
    { cat "$path"; printf '\n%s\n' "$record"; } \
      | psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --quiet --single-transaction
  fi
}

# ------------------------------------------------------------------- neo4j

neo4j_setup() {
  command -v cypher-shell >/dev/null 2>&1 || die "cypher-shell not found on PATH"
  : "${NEO4J_URI:?set NEO4J_URI (e.g. bolt://neo4j.railway.internal:7687)}"
  : "${NEO4J_USER:?set NEO4J_USER}"
  : "${NEO4J_PASSWORD:?set NEO4J_PASSWORD}"

  neo4j_run "CREATE CONSTRAINT schema_migration_filename IF NOT EXISTS
             FOR (m:_SchemaMigration) REQUIRE m.filename IS UNIQUE;"
}

neo4j_run() {
  cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
    --format plain "$@"
}

neo4j_applied_checksum() {
  neo4j_run "MATCH (m:_SchemaMigration {filename: '$1'}) RETURN m.checksum;" \
    | tail -n +2 | tr -d '"' | head -1
}

neo4j_apply() {
  local path="$1" name="$2" sum="$3"
  # cypher-shell runs each statement separately, so schema changes here are
  # not wrapped in one transaction. Migrations must be individually idempotent.
  cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
    --format plain -f "$path"
  neo4j_run "MERGE (m:_SchemaMigration {filename: '${name}'})
             ON CREATE SET m.checksum = '${sum}', m.applied_at = datetime();"
}

# -------------------------------------------------------------------- main

case "$TARGET" in
  postgres) EXT="sql" ;;
  neo4j)    EXT="cypher" ;;
esac

# Read into an array without mapfile, which macOS's bash 3.2 does not have.
MIGRATIONS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && MIGRATIONS+=("$line")
done < <(collect_migrations "$EXT")

# An empty directory is a valid state: Postgres currently has no Locus-owned
# schema (n8n manages its own tables). Only an explicit --file that resolves
# to nothing is an error, and collect_migrations already handles that.
if [[ ${#MIGRATIONS[@]} -eq 0 ]]; then
  echo "No *.${EXT} migrations in ${MIGRATION_DIR} — nothing to do."
  exit 0
fi

if [[ "$DRY_RUN" == false ]]; then
  "${TARGET}_setup"
fi

echo "== migration.sh: target=${TARGET} (${#MIGRATIONS[@]} candidate(s)) =="

applied_count=0
for path in "${MIGRATIONS[@]}"; do
  name="$(basename "$path")"
  sum="$(checksum "$path")"

  if [[ "$DRY_RUN" == true ]]; then
    echo " ? ${name} (dry run, not checked against the database)"
    continue
  fi

  recorded="$("${TARGET}_applied_checksum" "$name" | tr -d '[:space:]')"

  if [[ -n "$recorded" ]]; then
    if [[ "$recorded" == "$sum" ]]; then
      echo " = ${name} (already applied)"
      continue
    fi
    die "${name} changed after it was applied.
  recorded checksum: ${recorded}
  current checksum:  ${sum}
The live schema no longer matches this file. Write a new migration to
reconcile the difference instead of editing this one."
  fi

  echo " + ${name}"
  "${TARGET}_apply" "$path" "$name" "$sum"
  applied_count=$((applied_count + 1))
done

if [[ "$DRY_RUN" == true ]]; then
  echo "Dry run: nothing applied."
else
  echo "Applied ${applied_count} migration(s)."
fi
