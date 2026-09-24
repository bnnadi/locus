# hermes

Hermes Agent gateway (`nousresearch/hermes-agent:v2026.8.16`). Dashboard on
9119 is the public surface; the OpenAI API on 8642 stays internal.

On a public domain the image fails closed unless dashboard auth is set
(basic-auth, OAuth, or OIDC). Interactive OAuth parks the gateway
(`OAuthNonInteractiveError`, 2026-08-21) — use a bearer token. Attach a
volume at `/opt/data`. `numReplicas: 1`.

## Shadow team profiles

`profiles/` is the source for a kanban worker team. Install into a running
instance with `install-profiles.sh` (safe to re-run). Named profiles live
under `/opt/data/profiles/<name>` on the volume.

| Profile | Role | Writes |
|---|---|---|
| `default` | Orchestrator. Decomposes a goal into cards, routes, judges. No terminal or file tools. | Pull requests only (GitHub MCP bearer) |
| `researcher` | Answers a question. Only lane that reads the web (inside OpenCode, not Hermes). | Findings attachment. No source, tests, or git |
| `spec` | Writes the failing test first | Test files only |
| `engineer` | Makes the test pass | Source, not tests |
| `qa` | Runs the suite and verifies | Nothing |

Each worker sets `terminal.home_mode: profile` so OpenCode reads that
profile's `~/.config/opencode`. That is what keeps lanes from seeing each
other's agents. Do not put those agent files in a shared repo `.opencode/`.

`install-profiles.sh` installs `researcher`, `spec`, `engineer`, and `qa`,
then merges orchestrator config into `default` without overwriting the
volume's model pin. After install: set a model on each profile
(`hermes -p <name> model`) and confirm
`hermes -p default tools --summary` has no terminal, file, or code tools.

Most features are three cards (`spec` → `engineer` → `qa`) on a shared
`worktree:` or `dir:` workspace. Add a `researcher` card only when a real
unknown blocks a decision, and gate spec on it.
