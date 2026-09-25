#!/usr/bin/env bash
# Regression: .github/workflows/ci.yml installs ruff and pytest with plain
# `pip install <name>` -- no version -- and the repo has no ruff config, so
# the lint gate's rule set is whatever ruff's defaults happen to be on the
# day CI runs. That silently changed underneath the project (0 findings on
# ruff 0.6.9/0.9.10, 8 on 0.16.9), breaking lint on every branch including
# main since 2026-08-17 with nothing in any diff to explain it. A version
# range or wildcard (`ruff>=0.16`, `ruff==0.16.*`) is the same bug wearing a
# disguise: PEP 440 resolves either to "whatever is newest today". Pinning
# every tool install exactly, requiring an explicit non-empty ruff rule
# selection, and keeping the ruff config's target-version in step with the
# Python version CI actually runs closes both the incident and the class of
# bug it belongs to.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CI_FILE="$ROOT/.github/workflows/ci.yml"
RUFF_TOML="$ROOT/ruff.toml"
RUFF_TOML_HIDDEN="$ROOT/.ruff.toml"
PYPROJECT="$ROOT/pyproject.toml"

fail=0
fail_with() {
  printf 'FAIL %s: %s\n' "$1" "$2" >&2
  fail=1
}

# Borrowed verbatim from test_hermes_opencode_pin.sh: quotes are
# semantically irrelevant to a shell word or a pip requirement, so
# normalize them away rather than treating a quoted pin as a different
# value (e.g. `pip install "ruff==0.16.9"`).
strip_quotes() {
  local v="$1"
  case "$v" in
    \"*\") v="${v#\"}"; v="${v%\"}" ;;
    \'*\') v="${v#\'}"; v="${v%\'}" ;;
  esac
  printf '%s' "$v"
}

# An exact pin, allowing extras (ruff[all]==0.16.9). The version-side
# character class deliberately excludes '*': `==0.16.*` and `==0.16.9.*`
# are valid PEP 440 specs that resolve to "whatever is newest matching
# today", the same unpinned-lint-gate bug in a different disguise.
PIN_SHAPE_RE='^[A-Za-z0-9_.-]+(\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9_.+-]*$'

# --- read ci.yml: strip each physical line's trailing comment first (a
# '#' inside a quoted string would be mis-stripped too; none of the real
# shapes in this file need one), then join any line still ending in a
# backslash with the next physical line, remembering the first physical
# line number of each logical line so failures point at something
# actionable. Reports a labeled failure instead of crashing under set -e
# if the file has moved. ---
ci_available=1
ci_lines=()
ci_linenos=()
if [[ ! -r "$CI_FILE" ]]; then
  fail_with ci_readable "cannot read $CI_FILE"
  ci_available=0
else
  lineno=0
  pending=""
  pending_start=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    line="${line%$'\r'}"
    line="${line%%#*}"
    if [[ -n "$pending" ]]; then
      line="$pending $line"
    else
      pending_start=$lineno
    fi
    if [[ "$line" == *\\ ]]; then
      pending="${line%\\}"
    else
      ci_lines+=("$line")
      ci_linenos+=("$pending_start")
      pending=""
    fi
  done < "$CI_FILE"
  if [[ -n "$pending" ]]; then
    ci_lines+=("$pending")
    ci_linenos+=("$pending_start")
  fi
fi

# Matches a real `pip install` or `pip3 install` invocation and captures
# everything after it. The prefix group requires the command word to be
# preceded by start-of-line or a non-identifier character, and "pip3?" is
# "pip" with an optional literal "3", so "pipx install" never matches (no
# following whitespace) while "pip3 install" and "python -m pip install"
# both do; "uv pip install" already matched via the boundary and still does.
pip_install_re='(^|[^A-Za-z0-9_])pip3?[[:space:]]+install([[:space:]]+(.*))?$'

# Matches a real `ruff check` invocation and captures its arguments.
ruff_check_re='(^|[^A-Za-z0-9_])ruff[[:space:]]+check([[:space:]]+(.*))?$'

# Matches a `python-version: "X.Y"` declaration (actions/setup-python).
python_version_re='(^|[[:space:]])python-version:[[:space:]]*.?([0-9]+\.[0-9]+)'

ruff_check_found=0
ruff_check_dirs=()
ci_python_versions=()

if [[ "$ci_available" -eq 1 ]]; then
  for i in "${!ci_lines[@]}"; do
    raw="${ci_lines[$i]}"
    lineno="${ci_linenos[$i]}"
    trimmed="${raw#"${raw%%[![:space:]]*}"}"

    # A blank line, or a line that was nothing but a comment before the
    # trailing-comment strip above, is not a command of any kind.
    if [[ -z "$trimmed" ]]; then
      continue
    fi

    if [[ "$trimmed" =~ $ruff_check_re ]]; then
      ruff_check_found=1
      rc_args="${BASH_REMATCH[3]}"
      rc_tokens=()
      if [[ -n "$rc_args" ]]; then
        read -ra rc_tokens <<< "$rc_args"
      fi
      if [[ ${#rc_tokens[@]} -gt 0 ]]; then
        for rc_t in "${rc_tokens[@]}"; do
          case "$rc_t" in
            -*) continue ;;
          esac
          ruff_check_dirs+=("$rc_t")
        done
      fi
    fi

    if [[ "$trimmed" =~ $python_version_re ]]; then
      ci_python_versions+=("${BASH_REMATCH[2]}")
    fi

    # --- 1: every package on a `pip install`/`pip3 install` line in
    # ci.yml must carry an exact ==version, except two intentional gaps
    # walked token-by-token (not line-wide, so a tidy-up that collapses
    # several installs onto one line can't smuggle an unpinned package
    # past a line-level exemption): the bare `pip` token itself (covers
    # `python -m pip install --upgrade pip`), and flags that consume a
    # separate value where the pin lives in that value instead
    # (`-r/--requirement`, `-c/--constraint`, `--index-url`,
    # `--extra-index-url`, `-f/--find-links`) -- any other flag is simply
    # skipped, not treated as consuming a value. A `;`/`&&`/`||` token
    # ends this pip install's argument list the same way a trailing
    # comment does; anything chained after it on the same line is not
    # re-parsed as a second install.
    if [[ "$trimmed" =~ $pip_install_re ]]; then
      pi_args="${BASH_REMATCH[3]}"
      pi_tokens=()
      if [[ -n "$pi_args" ]]; then
        read -ra pi_tokens <<< "$pi_args"
      fi

      pi_clean=()
      if [[ ${#pi_tokens[@]} -gt 0 ]]; then
        for pt in "${pi_tokens[@]}"; do
          case "$pt" in
            '&&' | ';' | '||') break ;;
          esac
          pi_clean+=("$pt")
        done
      fi

      if [[ ${#pi_clean[@]} -eq 0 ]]; then
        fail_with ci_pip_pinned \
          "$CI_FILE:$lineno: 'pip install' with no packages or flags -- nothing to verify"
        continue
      fi

      n=${#pi_clean[@]}
      idx=0
      while [[ $idx -lt $n ]]; do
        t="${pi_clean[$idx]}"
        case "$t" in
          -r | --requirement | -c | --constraint | --index-url | --extra-index-url | -f | --find-links)
            idx=$((idx + 2))
            continue
            ;;
          --requirement=* | --constraint=* | --index-url=* | --extra-index-url=* | --find-links=*)
            idx=$((idx + 1))
            continue
            ;;
          -*)
            idx=$((idx + 1))
            continue
            ;;
        esac

        pkgspec="$(strip_quotes "$t")"
        if [[ "$pkgspec" == "pip" ]]; then
          idx=$((idx + 1))
          continue
        fi

        if [[ ! "$pkgspec" =~ $PIN_SHAPE_RE ]]; then
          pkgname="$(printf '%s' "$pkgspec" | sed -E 's/^([A-Za-z0-9_.-]+).*/\1/')"
          if [[ "$pkgspec" != *"=="* ]]; then
            reason="installed without an exact ==version pin"
          elif [[ "$pkgspec" == *'*'* ]]; then
            reason="pin contains a wildcard ('*'), which PEP 440 resolves to the newest matching release, not an exact version"
          else
            reason="pin is not a clean exact version (malformed spec)"
          fi
          fail_with ci_pip_pinned \
            "$CI_FILE:$lineno: package [$pkgname] $reason (found [$t])"
        fi
        idx=$((idx + 1))
      done
    fi
  done
fi

if [[ "$ruff_check_found" -eq 0 ]]; then
  # --- 5: nothing asserts ci.yml still runs the linter at all. A deleted
  # Lint step would otherwise leave a pinned ruff and an unread ruff.toml
  # as pure decoration -- the same shape as the opencode-declared-but-
  # never-expanded bug in the sibling test. ---
  fail_with ci_ruff_check_present \
    "$CI_FILE has no 'ruff check' invocation; a pinned ruff and a ruff config are decoration if nothing ever runs the linter"
fi

# --- 6: cross-check ci.yml's Python version against ruff's target-version
# once both are known (below); done once so a drift reports once instead
# of once per job that happens to declare python-version. ---
ci_python_version_ok=0
ci_python_version_normalized=""
ci_python_version_raw=""
if [[ ${#ci_python_versions[@]} -gt 0 ]]; then
  ci_unique_count="$(printf '%s\n' "${ci_python_versions[@]}" | sort -u | wc -l | tr -d ' ')"
  if [[ "$ci_unique_count" -gt 1 ]]; then
    fail_with ci_python_version_consistent \
      "multiple distinct python-version values declared in $CI_FILE: $(printf '%s ' "${ci_python_versions[@]}")"
  else
    ci_python_version_raw="${ci_python_versions[0]}"
    ci_python_version_normalized="py${ci_python_version_raw//./}"
    ci_python_version_ok=1
  fi
fi

# --- reads a file (or, for pyproject.toml, only its [tool.ruff...]
# sections) with each physical line's trailing comment stripped first, so
# a commented-out `select = [...]` or a `[tool.ruff] # comment` header is
# handled the same way ci.yml's comments are above. Same caveat: a '#'
# inside a quoted TOML string value would be mis-stripped too, which a
# ruff config has no reason to need. ---
read_ruff_toml_stripped() {
  local path="$1" out="" cline
  while IFS= read -r cline || [[ -n "$cline" ]]; do
    out="$out${cline%%#*}"$'\n'
  done < "$path"
  printf '%s' "$out"
}

read_pyproject_ruff_sections_stripped() {
  local path="$1" out="" pline stripped header in_ruff_section=0
  while IFS= read -r pline || [[ -n "$pline" ]]; do
    stripped="${pline%%#*}"
    if [[ "$stripped" =~ ^[[:space:]]*\[([A-Za-z0-9_.-]+)\][[:space:]]*$ ]]; then
      header="${BASH_REMATCH[1]}"
      case "$header" in
        tool.ruff | tool.ruff.*) in_ruff_section=1 ;;
        *) in_ruff_section=0 ;;
      esac
      continue
    fi
    if [[ "$in_ruff_section" -eq 1 ]]; then
      out="$out$stripped"$'\n'
    fi
  done < "$path"
  printf '%s' "$out"
}

# --- 2/4/6: a ruff config must exist and declare an explicit,
# non-empty rule selection (select/extend-select) plus a target-version
# that matches the Python version ci.yml actually runs, since a config
# that omits select still inherits ruff's shifting defaults -- the actual
# bug -- and an empty list ('select = []', or every entry commented out)
# is a gate checking zero rules, which is the same bug again. ---
ruff_config_text=""
ruff_config_source=""

if [[ -r "$RUFF_TOML" ]]; then
  ruff_config_text="$(read_ruff_toml_stripped "$RUFF_TOML")"
  ruff_config_source="$RUFF_TOML"
elif [[ -r "$RUFF_TOML_HIDDEN" ]]; then
  ruff_config_text="$(read_ruff_toml_stripped "$RUFF_TOML_HIDDEN")"
  ruff_config_source="$RUFF_TOML_HIDDEN"
elif [[ -r "$PYPROJECT" ]]; then
  section="$(read_pyproject_ruff_sections_stripped "$PYPROJECT")"
  if [[ -n "$section" ]]; then
    ruff_config_text="$section"
    ruff_config_source="$PYPROJECT ([tool.ruff...])"
  fi
fi

if [[ -z "$ruff_config_source" ]]; then
  fail_with ruff_config_exists \
    "no ruff config found (checked $RUFF_TOML, $RUFF_TOML_HIDDEN, and a [tool.ruff...] section in $PYPROJECT); without one, ruff's rule set is whatever its defaults happen to be that day -- 0 findings on 0.6.9/0.9.10, 8 on 0.16.9, same codebase"
else
  select_re='(^|[[:space:]])(extend-select|select)[[:space:]]*=[[:space:]]*\[([^]]*)\]'
  rule_code_re='["'"'"'][A-Za-z0-9]+["'"'"']'
  remaining="$ruff_config_text"
  select_key_present=0
  select_has_rule=0
  select_guard=0
  while [[ "$remaining" =~ $select_re ]] && [[ "$select_guard" -lt 20 ]]; do
    select_guard=$((select_guard + 1))
    select_key_present=1
    sel_body="${BASH_REMATCH[3]}"
    sel_match="${BASH_REMATCH[0]}"
    remaining="${remaining#*"$sel_match"}"
    if [[ "$sel_body" =~ $rule_code_re ]]; then
      select_has_rule=1
    fi
  done

  if [[ "$select_key_present" -eq 0 ]]; then
    fail_with ruff_config_select \
      "$ruff_config_source has no 'select' or 'extend-select' key; a config that omits it still inherits ruff's shifting defaults"
  elif [[ "$select_has_rule" -eq 0 ]]; then
    fail_with ruff_config_select_nonempty \
      "$ruff_config_source's select/extend-select has no rule codes (e.g. 'select = []', or every entry commented out); a gate checking zero rules is still the shifting-defaults bug"
  fi

  target_version_re='(^|[[:space:]])target-version[[:space:]]*=[[:space:]]*.?(py[0-9]+)'
  ruff_target_version_found=0
  if [[ "$ruff_config_text" =~ $target_version_re ]]; then
    ruff_target_version_found=1
    ruff_target_version="${BASH_REMATCH[2]}"
  else
    fail_with ruff_config_target_version \
      "$ruff_config_source has no 'target-version' key; the codebase is about to adopt PEP 604 'X | None' syntax, which depends on the targeted Python version"
  fi

  if [[ "$ruff_target_version_found" -eq 1 && "$ci_python_version_ok" -eq 1 ]]; then
    if [[ "$ruff_target_version" != "$ci_python_version_normalized" ]]; then
      fail_with ruff_target_version_matches_ci \
        "$ruff_config_source target-version=[$ruff_target_version] doesn't match the Python ci.yml runs (python-version: [$ci_python_version_raw], normalized [$ci_python_version_normalized])"
    fi
  fi

  # --- 10: ruff resolves config from the nearest ancestor of each file it
  # lints, so a stray ruff.toml/.ruff.toml/pyproject.toml declaring
  # [tool.ruff...] inside a tree ci.yml actually lints would silently
  # shadow the root config for exactly the files CI lints. ---
  if [[ "$ruff_check_found" -eq 1 && ${#ruff_check_dirs[@]} -gt 0 ]]; then
    for lint_dir in "${ruff_check_dirs[@]}"; do
      lint_target="$ROOT/$lint_dir"
      if [[ -d "$lint_target" ]]; then
        while IFS= read -r cfg; do
          if [[ "$(basename "$cfg")" == "pyproject.toml" ]]; then
            if grep -Eq '^[[:space:]]*\[tool\.ruff' "$cfg" 2>/dev/null; then
              fail_with ruff_config_shadow \
                "$cfg declares a [tool.ruff...] section inside the linted tree '$lint_dir'; ruff would use it instead of $ruff_config_source for exactly what CI lints"
            fi
          else
            fail_with ruff_config_shadow \
              "$cfg exists inside the linted tree '$lint_dir'; ruff would use it instead of $ruff_config_source for exactly what CI lints"
          fi
        done < <(find "$lint_target" -type f \( -name 'ruff.toml' -o -name '.ruff.toml' -o -name 'pyproject.toml' \) 2>/dev/null)
      fi
    done
  fi
fi

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "ok: ci.yml pins every pip-installed tool exactly, runs ruff check, and a ruff config selects explicit non-empty rules with a target-version matching CI's Python"
