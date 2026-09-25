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

| Profile | Role | Intended writes | Enforced? |
|---|---|---|---|
| `default` | Orchestrator. Decomposes a goal into cards, routes, judges. No terminal or file tools. | Pull requests | Cannot run commands: yes. PR-only: no — depends on PAT scope |
| `researcher` | Answers a question. Only lane that reads the web (inside OpenCode, not Hermes). | Findings attachment only | Yes, for the OpenCode agent — it has no interpreter |
| `spec` | Writes the failing test first | Test files | No — writes and runs tests, so it has code execution |
| `engineer` | Makes the test pass | Source, not tests | No — same reason |
| `qa` | Runs the suite and verifies | Nothing | No — running a suite executes repo code |

**Read [`SECURITY.md`](SECURITY.md) before relying on that table.** Three of
these lanes have arbitrary code execution by nature, which makes their `edit`
restrictions intent rather than containment, and any worker lane can read
`/opt/data/.env`. The controls that hold are the PAT scope, branch protection,
and your review of the PR.

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

Give a `researcher` card its own workspace, never the shared one. It is the
only lane taking instructions from outside the repo, and a shared tree would
let it leave an `AGENTS.md` that the engineer lane reads and acts on.

Run `install-profiles.sh` as root if you can. It pins each lane's OpenCode
permission files read-only and root-owned; run as the Hermes user, those files
stay writable by the same uid that runs the lane's tools, so a compromised
lane can rewrite its own permissions durably on the volume.
