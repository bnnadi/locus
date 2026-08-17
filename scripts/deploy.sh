#!/usr/bin/env bash
#
# Deploys the Locus stack to Railway, or brings it up locally with Compose.
#
# Usage:
#   scripts/deploy.sh --target railway [--service <name>] [--skip-migrations]
#   scripts/deploy.sh --target compose [--service <name>]
#
# The target can also come from LOCUS_DEPLOY_TARGET. There is no default:
# picking one silently is how you deploy to the wrong place.
#
# Railway reads each service's config from services/<name>/railway.json, which
# it only finds when that service's Root Directory is set to services/<name>.
# If a deploy ignores its railway.json, check that first.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TARGET="${LOCUS_DEPLOY_TARGET:-}"
ONLY_SERVICE=""
SKIP_MIGRATIONS=false

# Services built from this repo, in dependency order. Postgres and Neo4j are
# provisioned from Railway templates and are not built here.
SERVICES=(qdrant ollama n8n hermes hermes-memory-router)

die() {
  echo "error: $*" >&2
  exit 1
}

# Health path per service. A service with no path here is deployed but not
# gated on a readiness check.
health_path_for() {
  case "$1" in
    n8n)                  echo "/healthz/readiness" ;;
    hermes-memory-router) echo "/health" ;;
    ollama)               echo "/" ;;
    qdrant)               echo "/readyz" ;;
    *)                    echo "" ;;
  esac
}

# Base URL per service, read from the environment so the same script works
# against internal Railway hostnames and local Compose.
base_url_for() {
  case "$1" in
    n8n)                  echo "${N8N_URL:-}" ;;
    hermes-memory-router) echo "${HERMES_MEMORY_ROUTER_URL:-}" ;;
    ollama)               echo "${OLLAMA_URL:-}" ;;
    qdrant)               echo "${QDRANT_URL:-}" ;;
    hermes)               echo "${HERMES_URL:-}" ;;
    *)                    echo "" ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --service) ONLY_SERVICE="${2:-}"; shift 2 ;;
    --skip-migrations) SKIP_MIGRATIONS=true; shift ;;
    -h|--help) sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ -n "$TARGET" ]] || die "--target is required (railway|compose), or set LOCUS_DEPLOY_TARGET"
case "$TARGET" in
  railway|compose) ;;
  *) die "unsupported target: $TARGET (expected railway|compose)" ;;
esac

if [[ -n "$ONLY_SERVICE" ]]; then
  known=false
  for candidate in "${SERVICES[@]}"; do
    if [[ "$candidate" == "$ONLY_SERVICE" ]]; then
      known=true
    fi
  done
  [[ "$known" == true ]] \
    || die "unknown service: $ONLY_SERVICE (expected one of: ${SERVICES[*]})"
  SERVICES=("$ONLY_SERVICE")
fi

# Waits for a readiness endpoint, if one is defined and its URL is known.
wait_for_health() {
  local service="$1"
  local path base url
  path="$(health_path_for "$service")"
  base="$(base_url_for "$service")"

  if [[ -z "$path" ]]; then
    return 0
  fi
  if [[ -z "$base" ]]; then
    echo "   (no URL in the environment for ${service}; skipping health gate)"
    return 0
  fi

  url="${base%/}${path}"
  local waited=0
  while (( waited < 120 )); do
    if curl -sf --max-time 5 "$url" >/dev/null 2>&1; then
      echo "   ${service} healthy"
      return 0
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  die "${service} did not become healthy within 120s (${url})"
}

echo "== Locus deploy: target=${TARGET} =="

case "$TARGET" in

  railway)
    command -v railway >/dev/null 2>&1 || die "railway CLI not found on PATH"

    for service in "${SERVICES[@]}"; do
      echo "-- deploying ${service}"
      railway up --service "$service" --detach "services/${service}"
    done

    # Migrations run after deploy so the router image exists, but before the
    # health gate, because the router's readiness depends on the schema.
    if [[ "$SKIP_MIGRATIONS" == false ]]; then
      echo "-- running migrations"
      ./scripts/migration.sh --target postgres
      if [[ -n "${NEO4J_URI:-}" ]]; then
        ./scripts/migration.sh --target neo4j
      else
        echo "   NEO4J_URI unset, skipping Neo4j migrations"
      fi
    else
      echo "-- skipping migrations (--skip-migrations)"
    fi

    for service in "${SERVICES[@]}"; do
      echo "-- checking ${service}"
      wait_for_health "$service"
    done
    ;;

  compose)
    command -v docker >/dev/null 2>&1 || die "docker not found on PATH"

    # The override is passed explicitly because it lives in config/ rather
    # than next to the base file, so Compose will not auto-discover it.
    COMPOSE=(docker compose -f docker-compose.yml -f config/docker-compose.override.yml)

    if [[ -n "$ONLY_SERVICE" ]]; then
      echo "-- building and starting ${ONLY_SERVICE}"
      "${COMPOSE[@]}" up -d --build "$ONLY_SERVICE"
    else
      echo "-- building and starting the full stack"
      "${COMPOSE[@]}" up -d --build
    fi

    echo "-- Compose service state"
    "${COMPOSE[@]}" ps
    ;;

esac

echo "Deploy complete."
