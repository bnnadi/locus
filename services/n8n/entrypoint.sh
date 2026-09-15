#!/bin/sh
# Railway bind-mounts volumes as root. n8n's image runs as uid 1000 (node),
# so a mount at /home/node/.n8n is not writable and start dies with
# EACCES on .../config. Fix ownership, then drop privileges — n8n itself
# must not stay root.
#
# N8N_UPSTREAM_ENTRYPOINT is the official image entrypoint. Tests may
# point it at /bin/true so this script can be exercised without booting n8n.
set -e

N8N_USER_FOLDER="${N8N_USER_FOLDER:-/home/node/.n8n}"
# Must be exported: `su -c` is a child shell and will not see an unexported
# assignment. An empty expansion becomes `exec ""` and dies with
# `exec: line 0: : Permission denied`.
export N8N_UPSTREAM_ENTRYPOINT="${N8N_UPSTREAM_ENTRYPOINT:-/docker-entrypoint.sh}"

# Railway healthchecks and the edge use PORT. n8n only binds N8N_PORT.
# Compose never sets PORT. If both are set and they disagree, refuse to
# start — overwriting N8N_PORT would pass the probe on PORT while the
# dashboard target port (5678) still receives traffic.
align_n8n_port() {
  if [ -z "${PORT:-}" ]; then
    return 0
  fi
  if [ -z "${N8N_PORT:-}" ]; then
    export N8N_PORT="$PORT"
    return 0
  fi
  if [ "$N8N_PORT" = "$PORT" ]; then
    return 0
  fi
  echo "locus: PORT=${PORT} and N8N_PORT=${N8N_PORT} differ; set Railway PORT to match N8N_PORT and the dashboard target port. Refusing to start." >&2
  return 1
}

align_n8n_port || exit 1

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$N8N_USER_FOLDER"
  chown -R node:node "$N8N_USER_FOLDER"
  exec su node -s /bin/sh -c 'exec "$N8N_UPSTREAM_ENTRYPOINT" "$@"' su "$@"
fi

exec "$N8N_UPSTREAM_ENTRYPOINT" "$@"
