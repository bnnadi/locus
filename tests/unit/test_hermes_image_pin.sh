#!/usr/bin/env bash
# Regression: services/hermes/Dockerfile's FROM tag and config/env.example's
# HERMES_VERSION must agree, or the documented version stops describing the
# image actually built. This holds today (both are v2026.8.16) -- this is a
# latent-drift guard, written to keep passing now and catch a future
# one-sided bump. Split out of test_hermes_opencode_pin.sh: it is a
# different concern (base image vs. an installed CLI) and does not depend
# on OPENCODE_VERSION existing at all.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKERFILE="$ROOT/services/hermes/Dockerfile"
ENV_EXAMPLE="$ROOT/config/env.example"

fail=0
fail_with() {
  printf 'FAIL %s: %s\n' "$1" "$2" >&2
  fail=1
}

# --- Dockerfile side: find the first FROM instruction (not necessarily
# line 1 -- a leading `# syntax=` directive, or a future global ARG above
# it, must not confuse this), and pull the tag out of its image reference,
# handling `AS <stage>` and an `@sha256:<digest>` pin deliberately. ---
from_tag=""
from_available=1
if [[ ! -r "$DOCKERFILE" ]]; then
  fail_with dockerfile_readable "cannot read $DOCKERFILE"
  from_available=0
else
  from_line="$(grep -m1 -E '^[[:space:]]*FROM[[:space:]]+' "$DOCKERFILE" || true)"
  if [[ -z "$from_line" ]]; then
    fail_with from_line_present "no FROM instruction found in $DOCKERFILE"
    from_available=0
  else
    # Tokenize on whitespace via read (not unquoted word-splitting, which
    # a linter would flag): FROM's own arguments are always simple
    # whitespace-separated tokens.
    read -r _ tok2 tok3 _ <<< "$from_line"
    if [[ "${tok2:-}" == --* ]]; then
      # FROM --platform=... <image> [AS <stage>]
      image_ref="${tok3:-}"
    else
      image_ref="${tok2:-}"
    fi

    if [[ -z "$image_ref" ]]; then
      fail_with from_image_parsed "could not parse an image reference out of: $from_line"
      from_available=0
    else
      image_no_digest="$image_ref"
      digest_part=""
      if [[ "$image_ref" == *@sha256:* ]]; then
        digest_part="${image_ref#*@}"
        image_no_digest="${image_ref%@*}"
      fi

      last_segment="${image_no_digest##*/}"
      tag=""
      if [[ "$last_segment" == *:* ]]; then
        tag="${last_segment##*:}"
      fi

      if [[ -n "$digest_part" && -z "$tag" ]]; then
        # Pure digest pin (repo@sha256:...): stronger than a tag pin and
        # has no tag to compare against HERMES_VERSION. Not a failure.
        echo "skip: FROM is pinned by digest ($digest_part) with no tag to compare against HERMES_VERSION"
        from_available=0
      elif [[ -z "$tag" ]]; then
        fail_with from_tag_present "FROM has no tag (implicit :latest): $from_line"
        from_available=0
      else
        from_tag="$tag"
      fi
    fi
  fi
fi

# --- env.example side ---
hermes_version=""
env_available=1
if [[ ! -r "$ENV_EXAMPLE" ]]; then
  fail_with env_readable "cannot read $ENV_EXAMPLE"
  env_available=0
else
  hermes_version="$(sed -n 's/^HERMES_VERSION=\(.*\)$/\1/p' "$ENV_EXAMPLE" | head -n1)"
  if [[ -z "$hermes_version" ]]; then
    fail_with hermes_version_documented "no 'HERMES_VERSION=<value>' found in $ENV_EXAMPLE"
    env_available=0
  fi
fi

# --- compare, gated on both sides being available so a missing tag or a
# missing HERMES_VERSION reports once instead of cascading into a second,
# misleading "they don't match" failure. ---
if [[ "$from_available" -eq 1 && "$env_available" -eq 1 ]]; then
  if [[ "$from_tag" != "$hermes_version" ]]; then
    fail_with hermes_from_tag_matches \
      "Dockerfile FROM tag=[$from_tag] != env.example HERMES_VERSION=[$hermes_version]"
  fi
fi

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "ok: services/hermes/Dockerfile FROM tag matches HERMES_VERSION in config/env.example"
