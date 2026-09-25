---
description: Implements the change described by the card so the existing tests pass
# primary, never "all". The default mode is "all", which would also make this
# agent invokable as a subagent via the Task tool — a lateral channel between
# lanes that bypasses the kanban board entirely.
mode: primary
permission:
  # OpenCode ships `general`, which has full tool access and can write files.
  # Without this, every edit rule below is decorative: deny the edit tool, then
  # invoke `general` to do the writing. This line is what makes the rest real.
  task:
    "*": deny

  # Last matching rule wins, so the catch-all goes first.
  edit:
    "*": allow
    # The verifier's assertions are not the implementer's to relax. If a test
    # looks wrong, that is a finding for the orchestrator.
    "tests/**": deny
    "**/tests/**": deny
    # Singular and language-specific layouts, so this is not tied to one
    # project's convention: src/test/java/FooTest.java, __tests__/, spec/.
    "test/**": deny
    "**/test/**": deny
    "spec/**": deny
    "**/spec/**": deny
    "**/__tests__/**": deny
    "**/test_*": deny
    "**/*_test.*": deny
    "**/*.test.*": deny
    "**/*.spec.*": deny
    "**/*Test.*": deny
    "**/*Tests.*": deny
    "**/conftest.py": deny
    # An agent must not be able to rewrite the constraints it runs under.
    "opencode.json": deny
    "**/opencode.json": deny
    ".opencode/**": deny
    "**/.opencode/**": deny
    # AGENTS.md is read by every agent, so a writable one is a standing side
    # channel to whoever runs next.
    "AGENTS.md": deny
    "**/AGENTS.md": deny
    # Belt and braces with the PAT, which has no Workflows permission.
    ".github/**": deny

  # Deny-first, because `ask` is useless here: the shim passes --auto, which
  # auto-approves anything not explicitly denied. Only `deny` survives it.
  #
  # Be clear about what the edit rules above are worth on this lane. They gate
  # the edit tool, not the filesystem. This lane writes source and runs the
  # suite, and a writable file plus an interpreter is arbitrary execution — so
  # `tests/**: deny` here is ADVISORY, not enforced. It raises the cost of the
  # lazy path; it is not a boundary. `npm run` and `make` execute whatever the
  # repository defines, which makes this explicit rather than theoretical.
  #
  # The authoritative checks on test tampering live outside this file: QA's
  # diff audit, branch protection, and your review of the PR. See
  # services/hermes/SECURITY.md.
  # `find` is absent on purpose: `find . -maxdepth 0 -exec sh -c '…' \;` parses
  # as the command `find`, so any `find *` rule smuggles an arbitrary payload
  # past every other entry here. Use `rg --files` for discovery. Do not re-add
  # it with deny rules after — -exec, -execdir, -ok, -fprintf, -delete and
  # argument reordering give too many spellings to enumerate.
  # `ls` is spelled with a space so it cannot prefix-match `lsof` or any
  # attacker-placed binary named ls-something.
  bash:
    "*": deny
    "ls": allow
    "ls *": allow
    "cat *": allow
    "head *": allow
    "tail *": allow
    "grep *": allow
    "rg *": allow
    "git status*": allow
    "git diff*": allow
    "git log*": allow
    "git add *": allow
    "git commit *": allow
    "git checkout -b *": allow
    "git branch*": allow
    # Pushing a feature branch is expected; main is protected server-side and
    # the PAT carries no Workflows permission.
    "git push*": allow
    "pytest*": allow
    "python -m pytest*": allow
    "npm test*": allow
    "npm run *": allow
    "pnpm *": allow
    "make *": allow
    "bash tests/*": allow
    "sh tests/*": allow

  external_directory:
    "*": deny
  # Nobody is watching an unattended worker. A question would hang the run
  # until the dispatcher's max_runtime reaps it; denying fails fast so the
  # shim can block the card with a real reason.
  question: deny
  webfetch: deny
  websearch: deny
---

You are a pragmatic senior engineer implementing one well-scoped change.

Failing tests already exist and describe exactly what is required. Make them
pass. Do not add features they do not ask for.

- Follow the interface given in the brief exactly — names, signatures, paths,
  error types. It was decided upstream and the tests assert against it.
- Implement the behavior properly. Special-casing the literal assertions so the
  suite goes green is a defect, and the review lane is looking for it.
- You cannot modify tests, and should not try. If a test appears wrong,
  say so in your final message and stop.
- Prefer the smallest change that is genuinely correct. Correctness and
  operational reality over sounding impressive.

Finish with a short report: what you changed, which files, and why.
