# Shadow team: what the permission model does and does not enforce

The configs under `profiles/` read like access control. Much of it is not.
This file records which controls are real, which are advisory, and which gaps
have no fix at the configuration layer — so nobody (including a future you)
mistakes a comment in a YAML file for a boundary.

Written after a security audit of the `researcher` lane, 2026-09-24.

## The one-line version

Any lane that can write a file and run an interpreter has arbitrary code
execution, and arbitrary code execution voids every other rule in its config.
That describes `spec`, `engineer`, and `qa`. Their `edit` restrictions are
intent, not containment. `researcher` is the only lane where the restriction
is real, and only because it was stripped of every command that can execute.

The controls that actually hold the line are outside these files: the GitHub
PAT's scope, branch protection, and your review of the pull request.

## Enforced

These do what the comments claim:

- **`task: {"*": deny}` plus disabled built-in agents**, on every lane.
  Without it, `edit: deny` means nothing — OpenCode's built-in `general`
  subagent has full tool access and can write files, so any lane able to
  launch Task can escalate straight past its own permissions.
- **`mode: primary` on every agent.** The default is `all`, which would also
  make each agent invokable as a subagent, i.e. a lane-to-lane channel that
  skips the board.
- **The orchestrator cannot run commands or edit files.** `default` has no
  `terminal`, `file`, or `code_execution` toolset, expressed twice
  (`toolsets` allowlist and `agent.disabled_toolsets`).
- **`kanban.auto_decompose: false`.** Left on, the built-in Triage decomposer
  routes cards with its own auxiliary LLM — a second router that never
  consults `default`.
- **`researcher` cannot execute code.** It writes only `research/**` and its
  bash allowlist contains no interpreter, no test runner, and no `find`.
  This is the only lane whose write restriction is a boundary.

## Advisory only — do not count these

- **`spec`'s "tests only" and `engineer`'s `tests/**: deny`.** Both lanes
  write files and run the suite. `pytest` executes what they just wrote and
  auto-loads `conftest.py`; `npm run` and `make` execute whatever the repo
  defines. Either lane can reach any file if it tries. This is unavoidable:
  writing tests and running them is `spec`'s job.
- **`qa`'s `edit: deny`.** Same reason. Running a test suite executes
  repository code, so the read-only lane is read-only by convention.
- **Every `edit` glob.** They gate the edit *tool*, not the filesystem. A
  shell redirection (`cat x > src/app.py`) uses `cat`, which every lane
  allows. Whether OpenCode gates redirection targets is **untested** — see
  "Verify before trusting" below.
- **`default`'s instruction to treat findings as data.** It is a mitigation
  of unmeasurable efficacy, delivered in the same context window and the same
  modality as the attack it is meant to stop. Keep it as a statement of
  intent. It is not a control.
- **"Workers never create cards."** The dispatcher injects the kanban toolset
  at spawn, and that toolset includes `kanban_create` and `kanban_link`. The
  only thing stopping worker-to-worker routing is prompt text. Note that
  `disabled_toolsets: [kanban]` is NOT the fix — it would also strip
  `kanban_complete` and every card would be reaped as a protocol violation.

## No fix at this layer

Three gaps are structural. They are bounded, not closed.

### 1. No uid separation: any worker lane can read the PAT, and rewrite its own permissions

`cat /opt/data/.env` matches `cat *`. So does
`cat /opt/data/profiles/<name>/.env`, and `cat /proc/1/environ` reaches the
gateway's environment, which holds `API_SERVER_KEY` and the dashboard
password. `external_directory: deny` does not help — it gates the read and
edit tools, not bash. And `*` spans `/` in a bash pattern, so an absolute
path matches cleanly.

There is no allowlist of file-reading commands that is also a boundary around
a file. `"cat *.env": deny` is defeated by `cat /opt/data/./.env`,
`cd /opt/data && cat .env`, `tail -n +1`, `grep -h .`, and `/proc/*/environ`.

The same root cause makes a lane's own permission files writable by that lane.
`opencode.json` and `agents/<lane>.md` live on the volume and define every
restriction in the "Enforced" section above. `install-profiles.sh` writes them
mode 444 and root-owned, but the stock `hermes-agent` image runs tool
subprocesses as root too, and an owner can always chmod its own file. A lane
that rewrites them keeps the new permissions across the card, the session, and
a container restart.

**Proper fix:** run OpenCode tool subprocesses under a different uid than the
one owning `/opt/data/**/.env`, the gateway process, and the permission files.

**Until then:** treat every worker lane as trust-equivalent to the PAT, and
bound the damage at the token — fine-grained, one repository,
`contents:read` + `pull_requests:write`, no Workflows, short expiry. Mode 444
still earns its keep as a tripwire: hash the permission files after install
and alert on any change, since nothing legitimate rewrites them between runs.

### 2. The researcher has an outbound channel

`webfetch: allow` is an outbound request to a URL the agent chooses. A secret
read via gap 1 leaves in a query string, chunked across as many fetches as
needed. Omitting `curl`, `wget`, and `nc` removes redundant paths to a
capability already granted; it does not remove the capability.

This is inherent to a lane that reads arbitrary web pages. It can only be
bounded at the network layer, with an egress allowlist or a forward proxy
that permits approved hosts and strips query strings.

### 3. Second-order injection into the orchestrator

Hostile text in a findings attachment — or in a worker's completion summary,
which also wakes `default` — enters the context of the profile that holds the
PAT and can open pull requests. The highest-value move is not a rogue PR but
an attacker-authored *card body* routed to `engineer`, the lane that commits
and pushes.

No prompt wording closes this. The options, in increasing order of cost:

1. Scope the PAT so a hostile PR is inert, and require human review on
   `main`. Do this regardless; it is nearly free.
2. Require human approval for any card created in the same turn a findings
   attachment was read.
3. Split the roles so the profile that reads findings is not the profile that
   holds the PAT: a reader with no MCP, and a publisher that only ever
   receives a card id and a branch name.

## Verify before trusting

Unresolved questions about upstream behavior. Each one changes how much the
configs are worth, and none can be answered from this repo.

- **Does OpenCode gate shell redirection targets?** Test:
  `opencode run --agent qa --auto "run: cat /etc/hostname > /tmp/x"` and check
  whether `/tmp/x` exists. If it does not gate them, no allowlist of readers
  is safe and every `edit` rule in every lane is advisory.
- **Does OpenCode match `edit` paths relative or absolute?** The configs list
  both bare and `**/`-prefixed forms to be safe. If matching is absolute, the
  bare forms are dead weight; if relative, the prefixed ones are.
- **Does the dispatcher-injected kanban toolset include the orchestrator
  tools?** If it is the worker subset (`show`/`complete`/`block`/
  `heartbeat`), worker-to-worker routing is impossible and that row moves out
  of "advisory". The docs suggest the full set, in which case ask upstream for
  a per-profile kanban tool allowlist.
- **Does Hermes support a per-profile command allowlist for `terminal`?** If
  so, mirror each lane's OpenCode bash list there. Right now the shim that
  invokes OpenCode is the least restricted process in its own lane.
- **Does the GitHub MCP honor `X-MCP-Toolsets`?** If not, the PAT scope is the
  only thing keeping `default` from writing repository content.

## Required secrets

`GITHUB_PAT` is required by the `default` profile and is consumed by
`mcp_servers.github.headers`. It belongs in `/opt/data/.env` on the volume or
in the Railway service variables — never in this repo. Scope as described in
gap 1.
