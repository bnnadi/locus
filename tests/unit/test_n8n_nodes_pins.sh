#!/usr/bin/env bash
# Dependency versions in the n8n node package are exact. A range moves the
# SDK without a diff in the lockfile review.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG="$ROOT/services/n8n/nodes/package.json"

python3 - "$PKG" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
bad = []
for section in ("dependencies", "devDependencies", "peerDependencies"):
    for name, version in (doc.get(section) or {}).items():
        if any(token in version for token in ("^", "~", "*", ">", "<", " ")):
            bad.append(f"{section}.{name}={version}")
if bad:
    print("n8n node package has ranged versions:")
    print("\n".join(bad))
    sys.exit(1)
print("n8n node package versions are exact")
PY
