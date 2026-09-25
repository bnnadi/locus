#!/usr/bin/env bash
# Regression: profiles under services/hermes/profiles/ shell out to the
# opencode CLI, but nothing installs it. Once it is, an unpinned version
# (a bare "latest"/"main" ARG, or a curl-pipe-to-shell installer with no
# version reference) means a rebuild can silently change agent behavior,
# and a version documented in config/env.example that drifts from the one
# actually baked into the image is worse than documenting no version at all.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKERFILE="$ROOT/services/hermes/Dockerfile"
ENV_EXAMPLE="$ROOT/config/env.example"

fail=0
check() {
  local label="$1" condition="$2" message="$3"
  if [[ "$condition" -ne 0 ]]; then
    printf 'FAIL %s: %s\n' "$label" "$message" >&2
    fail=1
  fi
}

dockerfile_opencode_version="$(sed -n 's/^ARG OPENCODE_VERSION=\(.*\)/\1/p' "$DOCKERFILE" | head -n1)"
env_opencode_version="$(sed -n 's/^OPENCODE_VERSION=\(.*\)/\1/p' "$ENV_EXAMPLE" | head -n1)"
env_hermes_version="$(sed -n 's/^HERMES_VERSION=\(.*\)/\1/p' "$ENV_EXAMPLE" | head -n1)"
dockerfile_from_tag="$(sed -n '1s/^FROM [^:]*:\(.*\)$/\1/p' "$DOCKERFILE")"

# 1. Dockerfile pins OPENCODE_VERSION to something other than a floating tag.
if [[ -z "$dockerfile_opencode_version" ]]; then
  check opencode_arg_pinned 1 "no 'ARG OPENCODE_VERSION=<value>' found in $DOCKERFILE"
elif [[ "$dockerfile_opencode_version" == "latest" || "$dockerfile_opencode_version" == "main" ]]; then
  check opencode_arg_pinned 1 "ARG OPENCODE_VERSION is unpinned (got [$dockerfile_opencode_version])"
else
  check opencode_arg_pinned 0 ""
fi

# 2. env.example documents the same variable, non-empty, following the
# existing POSTGRES_VERSION/NEO4J_VERSION/HERMES_VERSION convention.
if [[ -z "$env_opencode_version" ]]; then
  check opencode_env_documented 1 "no 'OPENCODE_VERSION=<value>' found in $ENV_EXAMPLE"
else
  check opencode_env_documented 0 ""
fi

# 3. The Dockerfile default and the env.example value must be identical, or
# the documented pin no longer describes the image actually built.
if [[ -n "$dockerfile_opencode_version" && -n "$env_opencode_version" ]]; then
  if [[ "$dockerfile_opencode_version" != "$env_opencode_version" ]]; then
    check opencode_versions_match 1 \
      "Dockerfile ARG OPENCODE_VERSION=[$dockerfile_opencode_version] != env.example OPENCODE_VERSION=[$env_opencode_version]"
  else
    check opencode_versions_match 0 ""
  fi
else
  check opencode_versions_match 1 \
    "cannot compare: Dockerfile=[$dockerfile_opencode_version] env.example=[$env_opencode_version]"
fi

# 4. Latent drift guard: the base image tag must already agree with
# HERMES_VERSION in env.example. Holds today; must keep holding.
if [[ -z "$dockerfile_from_tag" || -z "$env_hermes_version" ]]; then
  check hermes_from_tag_matches 1 \
    "cannot compare: FROM tag=[$dockerfile_from_tag] HERMES_VERSION=[$env_hermes_version]"
elif [[ "$dockerfile_from_tag" != "$env_hermes_version" ]]; then
  check hermes_from_tag_matches 1 \
    "Dockerfile FROM tag=[$dockerfile_from_tag] != env.example HERMES_VERSION=[$env_hermes_version]"
else
  check hermes_from_tag_matches 0 ""
fi

# 5. A curl-pipe-to-shell installer with no version reference is the
# unpinned case wearing a disguise. Skip gracefully if no such installer is
# present, since a package-manager or direct-binary install is equally valid.
pipe_lines="$(grep -nE '(curl|wget)[^|]*\|[[:space:]]*(sh|bash)\b' "$DOCKERFILE" || true)"
if [[ -n "$pipe_lines" ]]; then
  if ! grep -qE 'OPENCODE_VERSION' <<<"$pipe_lines"; then
    check opencode_installer_pinned 1 \
      "curl/wget-piped-to-shell installer has no OPENCODE_VERSION reference: $pipe_lines"
  else
    check opencode_installer_pinned 0 ""
  fi
else
  echo "skip: no curl/wget-piped-to-shell installer present in $DOCKERFILE"
fi

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "ok: opencode is pinned identically in services/hermes/Dockerfile and config/env.example"
