#!/usr/bin/env bash
# Regression: a root-owned /home/node/.n8n (Railway's volume default, and
# the leftover from the old USER root Dockerfile) must become writable by
# uid 1000 before n8n starts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="locus-n8n-perm-test:local"
VOLUME="locus-n8n-perm-test"

if ! command -v docker >/dev/null 2>&1; then
  echo "skip: docker not on PATH"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "skip: docker daemon not reachable"
  exit 0
fi

cleanup() {
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -t "$IMAGE" "$ROOT/services/n8n"
docker volume rm "$VOLUME" >/dev/null 2>&1 || true
docker volume create "$VOLUME" >/dev/null

# Empty config + root:root 0755 dir is the Railway failure mode:
# writeFileSync('/home/node/.n8n/config') → EACCES.
docker run --rm -v "${VOLUME}:/data" alpine sh -c \
  'touch /data/config && chown -R root:root /data && chmod 755 /data && chmod 644 /data/config'

docker run --rm \
  -e N8N_UPSTREAM_ENTRYPOINT=/bin/true \
  -v "${VOLUME}:/home/node/.n8n" \
  "$IMAGE"

owner="$(docker run --rm -v "${VOLUME}:/data" alpine stat -c '%u:%g' /data /data/config | tr '\n' ' ')"
owner="${owner% }"
if [[ "$owner" != "1000:1000 1000:1000" ]]; then
  printf 'FAIL volume owner after entrypoint: [%s] expected [1000:1000 1000:1000]\n' "$owner" >&2
  exit 1
fi

# Reset to an empty root-owned dir so the full boot hits writeFileSync
# EACCES, not the leftover empty config file from the chown probe.
docker run --rm -v "${VOLUME}:/data" alpine sh -c \
  'rm -f /data/config; chown root:root /data; chmod 755 /data'

# Production path: do not set N8N_UPSTREAM_ENTRYPOINT. A missing export
# used to exec an empty command (`exec: line 0: : Permission denied`).
docker rm -f locus-n8n-perm-boot >/dev/null 2>&1 || true
docker run -d --name locus-n8n-perm-boot \
  -e N8N_ENCRYPTION_KEY=ci-not-a-real-key \
  -e N8N_DIAGNOSTICS_ENABLED=false \
  -v "${VOLUME}:/home/node/.n8n" \
  "$IMAGE" >/dev/null
trap 'docker rm -f locus-n8n-perm-boot >/dev/null 2>&1 || true; cleanup' EXIT

boot_ok=0
for _ in $(seq 1 45); do
  boot_logs="$(docker logs locus-n8n-perm-boot 2>&1 || true)"
  if echo "$boot_logs" | grep -q 'exec: line 0: :'; then
    printf 'FAIL upstream entrypoint was empty under su\n%s\n' "$boot_logs" >&2
    exit 1
  fi
  if echo "$boot_logs" | grep -q 'EACCES: permission denied, open'; then
    printf 'FAIL n8n still EACCES on config\n%s\n' "$boot_logs" >&2
    exit 1
  fi
  if echo "$boot_logs" | grep -q 'Editor is now accessible'; then
    boot_ok=1
    break
  fi
  sleep 1
done
docker rm -f locus-n8n-perm-boot >/dev/null 2>&1 || true
trap cleanup EXIT
if [[ "$boot_ok" -ne 1 ]]; then
  printf 'FAIL n8n did not start:\n%s\n' "$boot_logs" >&2
  exit 1
fi

echo "n8n entrypoint chowns a root-owned /home/node/.n8n to uid 1000"
echo "n8n starts on that volume (upstream entrypoint is exported into su)"
