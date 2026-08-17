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

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$N8N_USER_FOLDER"
  chown -R node:node "$N8N_USER_FOLDER"
  exec su node -s /bin/sh -c 'exec "$N8N_UPSTREAM_ENTRYPOINT" "$@"' su "$@"
fi

exec "$N8N_UPSTREAM_ENTRYPOINT" "$@"
