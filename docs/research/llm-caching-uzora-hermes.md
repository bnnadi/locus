# LLM caching for Uzora and Hermes: preliminary research

Status: **preliminary, adversarially reviewed.** Not a plan. Nothing here has
been implemented or run against the live Railway deployment. Researched
2026-09-24 on branch `feat/hermes-souls`; revised the same day after a
fact-check and an architecture critique.

Trigger: Braintrust, "LLM gateway caching: how it works and when to use it"
(9 Aug 2026), <https://www.braintrust.dev/articles/llm-gateway-caching>.

## TL;DR

- **Uzora makes no LLM calls and should not become an LLM gateway.** It is an
  *MCP* gateway. Later, cache `tools/list`, never `tools/call`.
- **Hermes and OpenCode already do provider prompt caching** for Claude. The
  job is measuring it, not adding a gateway.
- **Hermes turns on OpenRouter response caching by default** (300 s, scoped per
  API key). Whether Locus uses OpenRouter is on the volume, not in the repo.
  Make it explicit per profile.
- **The most urgent finding is not about caching:** `extract_claude` has three
  Sonnet 5 incompatibilities, and `claude` is the default backend, so
  `POST /traces` likely fails on every default call today (not confirmed live).
- **Second-most urgent:** `/traces` is not idempotent on a re-posted
  `trace_id`; retries double-count strategy success/failure.
- **Per-service provider keys with spend caps** give attribution, budgets and
  per-key cache metrics without any gateway.
- **No Braintrust/LiteLLM response cache and no semantic cache** in front of
  the agents.

## Adversarial review: what changed and why

Two reviewers checked the first draft: a fact-check against hermes-agent
`v2026.8.16` (`df4b65147d7d`) and opencode `v1.18.32`, and an architecture
critique. 8 of 10 load-bearing claims were confirmed. Changes:

| # | Change | Why |
|---|---|---|
| 1 | §3a gate list now includes "any Anthropic-wire endpoint + Claude-named model auto-caches" | `agent_runtime_helpers.py` ≈2378–2380 returns `(True, True)` for a "Third-party Anthropic-compatible gateway". The draft's "a gateway disables Hermes prompt caching" is only true on the OpenAI (`chat_completions`) wire |
| 2 | R1 no longer relies on the `💾 Prompt caching: ENABLED` line | It is printed only when `not agent.quiet_mode` (`agent_init.py:1547`), and gateway/API-server agents run `quiet_mode=True` (`gateway/run.py:5483`, `gateway/platforms/api_server.py:2925`). Use `usage.cache_read_input_tokens` |
| 3 | Sonnet 5 issue now cites the migration guide and lists three breakages | Temperature 400, adaptive thinking on by default (read content by type), `max_tokens` bounds thinking + text |
| 4 | Stated the fix for §5 outranks all caching work (new R1b) | The default backend is `claude` (`main.py:87`, `config/env.example`), so the memory loop is probably down |
| 5 | Added trace_id idempotency bug (new R2) | `ON MATCH` increments counters on every re-post (`main.py` ≈237–240), skewing `success_rate` used by `/retrieve` |
| 6 | R4 (memoization) reshaped: drift fix first, optional keyed skip later | Skip key omitted backend, model, prompt template and `outcome`; first-writer-wins is order-dependent, so the draft's claim that it "aligns with determinism" was wrong. A naive skip would also make CI stop exercising real extraction |
| 7 | R3 (1 h TTL) deferred | The break-even ignored that tail breakpoints are also written at 2× every turn, and it is unverified that a kanban resume reuses a byte-identical system prompt |
| 8 | R2→R4 (OpenRouter cache) rationale reworded; YAML preferred over env | A replay cannot re-run an already-executed tool call (the next request contains its result). The real point is making hidden, TTL-bounded state explicit and keeping it in the repo |
| 9 | R1 made executable | `hermes -p <name> model` *sets* the model; the draft used it as a read |
| 10 | New M1: per-service keys + spend caps | Attribution and budgets without a gateway |
| 11 | Uzora verdict sharpened, with a revisit trigger | See §2 |
| 12 | Smaller corrections | OpenRouter cache is per API key; Anthropic isolation is per workspace only on Claude API / Claude Platform on AWS / Foundry (per org on Bedrock / Vertex); Braintrust failover requests are never cached (`x-bt-cached: N/A`); "`/v1/messages` not cached by Braintrust" is inference from the path list; memory-router drift also reaches the API response |
| 13 | Added: `/traces` has no inbound auth | Injected `raw_reasoning` becomes a persistent, served strategy |

Bugs found here are being filed as tickets separately; this doc only records
them.

## 1. What each caching type actually is

| Type | Where it lives | What is reused | Output billed on hit? | Staleness risk |
|---|---|---|---|---|
| Exact-match response cache | Gateway in front of the provider | Whole response | No | High — replays a stored answer |
| Semantic cache | Gateway + embedding model + vector store | Whole response, for a *similar* request | No | Highest — can answer a different question |
| Provider prompt cache | Inside the provider | Computation for an identical prompt *prefix* | Yes — a new response is generated | None for the answer |

Provider prompt caching cannot return a wrong answer; the two response caches
can.

### Facts verified from primary sources

**Braintrust Gateway** (<https://www.braintrust.dev/docs/deploy/gateway>,
"Enable caching"):

- `x-bt-use-cache: auto | always | never`; `auto` caches only when
  `temperature=0` or `seed` is set.
- Cacheable paths: `/auto`, `/embeddings`, `/chat/completions`, `/completions`,
  `/moderations`. The page also documents an Anthropic-SDK entry point; that
  `/v1/messages` is *not* response-cached is an inference from the path list.
- Failover requests are never cached (`x-bt-cached: N/A`).
- `x-bt-cache-ttl` 1–604800 s (default 1 week); `Cache-Control: no-cache,
  no-store` ≡ `never`. Headers `x-bt-cached: HIT|MISS`, `Age`.
- AES-GCM with a key derived from the caller's API key (per-user; orgs can opt
  into sharing). A self-hosted data plane exists.
- "The Gateway does not translate provider caching settings across providers."

**Anthropic prompt caching**
(<https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>):

- Automatic (top-level `cache_control`) or explicit breakpoints; max 4, 20-block
  lookback. TTL 5 min (refreshed free on hit) or 1 h.
- Write 1.25× (5 m) / 2× (1 h) base input; read 0.1× (0.05× Opus 5.5, 0.025×
  Fable 5.1 / Mythos 5.1).
- Minimum prefix 1,024 tokens for Sonnet 5 / Opus 4.8; 512 for Opus 5.5 /
  Opus 5 / Fable; 4,096 for Haiku 4.5. Below it, nothing is cached, silently.
- Verify with `usage.cache_creation_input_tokens` / `cache_read_input_tokens`.
- Isolation: per organization everywhere; additionally per workspace on the
  Claude API, Claude Platform on AWS and Microsoft Foundry; Bedrock and Google
  Cloud are per organization only.

**OpenAI prompt caching**
(<https://platform.openai.com/docs/guides/prompt-caching>): automatic; minimum
1,024 visible tokens on GPT-5.6+; hits in
`usage.input_tokens_details.cached_tokens`; not shared across organizations.

**OpenRouter response caching**
(<https://openrouter.ai/docs/guides/features/response-caching>):
`X-OpenRouter-Cache: true`, TTL 1–86400 s (default 300). Key = API key + model
+ endpoint + streaming mode + SHA-256 of the body; **scoped per API key**.
"Returned verbatim regardless of stochastic parameters like `temperature`."
Hits show `X-OpenRouter-Cache-Status: HIT`. Unavailable under account-level ZDR.

**LiteLLM** (<https://docs.litellm.ai/docs/proxy/caching>): exact-match and
semantic caches; its docs say semantic caching "goes badly wrong on agentic
traffic".

## 2. Uzora: MCP gateway, not LLM gateway

Read from `git show feat/uzora-mcp-gateway:services/uzora/{main.py,README.md,auth.py}`
(not on `main` or this branch).

Uzora v0 is a JWT token gate: `/health`, `/whoami` (verifies an
Authentik-issued RS256 JWT), and `/mcp` returning 501. No LLM calls.

An **LLM gateway** sits between an agent and a model provider and sees
prompts and completions. An **MCP gateway** sits between an agent and tool
servers and sees JSON-RPC `tools/list` / `tools/call`
(<https://modelcontextprotocol.io/specification/2025-06-18/server/tools>). The
article's three caching types are LLM-gateway features.

**Should Uzora become the LLM gateway too? No, for concrete reasons:**

- **Attribution would be theatre.** Per-caller JWTs would be presented from
  inside the Hermes container, where every lane can read every other lane's
  env (`services/hermes/SECURITY.md`, gap 1). Uzora could not tell lanes apart.
- **M1 (§8) already gives per-service attribution** for free, at the provider.
- **Fidelity burden.** A model proxy must faithfully pass Anthropic
  `/v1/messages` SSE streaming and `cache_control`, or it silently degrades
  Hermes and OpenCode's prompt caching (§3a).
- **Maturity.** Uzora today fetches JWKS on every request uncached (`auth.py`
  on that branch); it is not yet a hardened hot-path proxy.
- **Coupling.** "Authenticate MCP tool access" and "broker model credentials"
  are different trust decisions; one service holding both widens its blast
  radius.

The one genuine argument for is **credential brokering** (lanes never hold the
provider key). Revisit trigger: if the egress proxy that SECURITY.md gap 2
calls for is built, evaluate brokering there, as its own service.

If `/mcp` is implemented: `tools/list` is cacheable with
`notifications/tools/list_changed` as invalidation; `tools/call` is not (live
state, and caching writes conflicts with
`human-approval-for-irreversible-actions`). `readOnlyHint` / `idempotentHint`
cannot gate it: the spec says clients "MUST consider tool annotations to be
untrusted unless they come from trusted servers".

## 3. Hermes: what the pinned image already does

Pinned `nousresearch/hermes-agent:v2026.8.16` (`services/hermes/Dockerfile`);
source read from tag `v2026.8.16` (commit `df4b65147d7d`) of
<https://github.com/NousResearch/hermes-agent>.

### 3a. Anthropic prompt caching: already automatic

`agent/agent_runtime_helpers.py::anthropic_prompt_cache_policy` enables
`cache_control` for:

- native Anthropic (Anthropic wire + provider `anthropic` or host
  `api.anthropic.com`) — native layout;
- OpenRouter or Nous Portal with a Claude/Kimi model on the OpenAI wire —
  envelope layout;
- **any Anthropic-wire endpoint with a Claude-named model** (≈ line 2378,
  "Third-party Anthropic-compatible gateway") — native layout;
- an explicit `prompt_caching: true` route+model declaration, needed only for
  bare model aliases (`website/docs/user-guide/configuring-models.md`
  ≈199–216), and consulted for Anthropic-wire and LiteLLM OpenAI-wire routes.

A custom endpoint on the **OpenAI (`chat_completions`) wire** that is not
OpenRouter/Portal/LiteLLM falls through to `(False, False)` (≈2440–2448): no
cache markers. So a Braintrust `/chat/completions` base URL would lose Hermes
prompt caching; an Anthropic-wire gateway would not.

Layout (`agent/prompt_caching.py`): 4 breakpoints — static system prefix, end of
system prompt, last 2 messages. `agent/system_prompt.py` builds the system
prompt once per session, stable → context → volatile (the volatile tier
includes a timestamp).

```yaml
prompt_caching:
  cache_ttl: "5m"   # or "1h"; a falsy value disables prompt caching
```

The startup line `💾 Prompt caching: ENABLED` is **not** a reliable check in
Locus: it is suppressed in `quiet_mode`, which the gateway and API server use.

### 3b. OpenRouter response caching: on by default

`hermes_cli/config_defaults.py` (≈866) ships `openrouter.response_cache: true`,
`response_cache_ttl: 300`. `DEFAULT_CONFIG` is merged under the user config
(`hermes_cli/config.py::_load_config_impl`). `build_or_headers` adds
`X-OpenRouter-Cache: true` to the **main agent client** whenever the base URL
host is `openrouter.ai` (`agent/agent_init.py` ≈1231, `run_agent.py` ≈6244),
and to auxiliary clients. Env overrides `HERMES_OPENROUTER_CACHE` /
`HERMES_OPENROUTER_CACHE_TTL`.

### 3c. Where model/provider are set

On the volume, not in the repo (`install-profiles.sh`, profile `config.yaml`
comments). The repo's profile configs set neither `prompt_caching` nor
`openrouter`, so every profile runs Hermes defaults. `extra_headers` exists per
provider but only on OpenAI-compatible routes (`configuring-models.md`).

## 4. OpenCode (worker lanes)

Lanes are Hermes shims that shell out to OpenCode (`services/hermes/README.md`).
OpenCode `1.18.32` is pinned in the working-tree Dockerfile and
`config/env.example` (uncommitted at time of writing). Repo `opencode.json`
files set permissions and agents only; credentials come from `/connect`, stored
in `~/.local/share/opencode/auth.json` (<https://opencode.ai/docs/providers/>),
under each profile's `home/`.

From `packages/opencode/src/provider/transform.ts` at `v1.18.32`
(<https://github.com/sst/opencode>): `applyCaching` marks the first 2 system and
last 2 messages `ephemeral` whenever the provider or model id contains
`anthropic`/`claude` (not on `@ai-sdk/gateway`); OpenAI-family SDKs get
`promptCacheKey = sessionID`. `baseURL` is overridable per provider.

## 5. hermes-memory-router

`services/hermes-memory-router/main.py`: `extract_claude` calls
`claude-sonnet-5` with `temperature=0`, `max_tokens=500`, and reads
`resp.content[0].text` (≈120–128). `extract_ollama` uses local `mistral`.
`strategy_id_for` (`identity.py`) derives the id from `strategy_key`, or
`task_type` + normalized `raw_reasoning`.

**5.1 Sonnet 5 breaks `extract_claude` three ways.** From the migration guide
(<https://platform.claude.com/docs/en/models/sonnet-5/migration-guide>):

1. "sampling parameters (`temperature`, `top_p`, `top_k`) set to non-default
   values return a 400 error".
2. Adaptive thinking is on by default, so responses "can now return `thinking`
   blocks before the first `text` block and code that reads content by position
   must select content blocks by `type`". `content[0].text` is unsafe.
3. "`max_tokens` remains a hard limit on total output (thinking plus response
   text)", so 500 can starve the JSON answer. The new tokenizer (~30% more
   tokens) tightens it further.

`claude` is the default backend (`main.py:87`, `config/env.example`), so every
default `/traces` call likely returns 502 today. Not confirmed live.
`docs/troubleshooting.md` already lists 502 from `/traces` with an "invalid
Anthropic model id" as a cause, which may be a misdiagnosis of this.

**5.2 `/traces` is not idempotent.** `ReasoningTrace` is merged `ON CREATE`
only, but `StrategyItem ON MATCH` increments success/failure counts
unconditionally (≈237–240). A client retry with the same `trace_id` double-counts
and skews `success_rate`, which `/retrieve`'s `min_success_rate` filter uses.
This needs an idempotency short-circuit, not a response cache.

**5.3 Drift between stores.** When a `strategy_id` already exists, Neo4j keeps
the first extraction's title/description, but the Qdrant embedding
(≈264–285) and the API response (≈292–299) use the *new* extraction. Three
views of one strategy can disagree.

**5.4 No inbound auth on `/traces`.** Only Neo4j credentials exist (≈79). Any
caller on the network can post `raw_reasoning` that becomes a persistent,
retrievable strategy served to agents — a second-order prompt-injection path.
Any "reuse the stored extraction" design makes such an entry stickier.

**5.5 Prompt caching cannot help here.** The fixed template is far below the
1,024-token minimum, and the rest varies per trace.

## 6. Component × caching type

| Component | Exact-match response cache | Semantic cache | Provider prompt cache |
|---|---|---|---|
| Uzora | Does not apply — no LLM calls | Does not apply | Does not apply |
| Hermes orchestrator (`default`) | Conditional: on by default if OpenRouter; else near-zero hits | No | Applies; already automatic (unless OpenAI-wire custom endpoint) |
| Hermes worker shims | Same as above | No | Same as above |
| OpenCode agents in lanes | Near-zero hits | No | Applies; OpenCode adds markers for Claude |
| memory-router `extract_claude` | Only as a later, keyed skip (R5 step 2) | No | Does not apply (no stable prefix ≥ 1,024) |
| memory-router embeddings | Skip — MiniLM on CPU, loaded once (≈84), milliseconds | No | n/a |
| Ollama | Covered by R5 if ever used | No | Only `OLLAMA_KEEP_ALIVE` model residency (<https://github.com/ollama/ollama/blob/main/docs/faq.md>), and only if the ollama backend is used |

## 7. Why a gateway response cache is a poor fit for the agents

**Hit rate.** An exact-match key is the whole body: full transcript, tool
results, a system prompt with a timestamped volatile tier. Collisions are
limited to the first turn of a quickly retried card. Expected hit rate
≈ zero (reasoning, not measured).

**What a hit means.** A replay is not dangerous in the "re-runs a tool" sense:
the next request carries the tool result, so it differs. The cost is that a
retried card gets the same sample instead of a fresh one, and behaviour
depends on hidden, TTL-bounded, evictable state.

**Determinism.** A cache hit does not make outputs reproducible from inputs;
it makes them depend on what happened to be cached. The `determinism`
principle wants the former.

**Security.** SECURITY.md treats every lane as trust-equivalent to the PAT and
model keys (gap 1). A hosted gateway adds a third party seeing every prompt
(repo code, tool output, anything read off disk) and another readable key.
Braintrust's "cannot see your data" is a vendor claim, not verified.

**Loses what works.** On the OpenAI wire, a custom gateway URL turns off
Hermes prompt caching (§3a).

**Cost and ops.** LiteLLM needs a new service plus Redis. Semantic caching adds
an embedding model and vector store; sharing Qdrant crosses
`service-isolation`.

**Evals / CI.** Locus has no LLM eval harness, the one workload where exact
match shines. For CI, prefer a stubbed extraction backend plus one real smoke
test over an exact-match cache. Braintrust as observability-only is premature.

## 8. Ranked recommendations

None implemented. Bugs in R1b and R2 are being ticketed separately.

### R1. Measure what is live, and check Sonnet 5 with one call

Executable steps (read-only; never print keys):

1. `railway ssh` into Hermes, then
   `grep -E '^(model|provider|base_url)|openrouter|prompt_caching' /opt/data/profiles/*/config.yaml`
   (and the root `/opt/data/config.yaml`).
2. List env var **names** only for OpenRouter/cache overrides, e.g.
   `env | cut -d= -f1 | grep -E 'OPENROUTER|HERMES_.*CACHE'`.
3. For each lane, list provider **names** only from
   `/opt/data/profiles/<name>/home/.local/share/opencode/auth.json`
   (e.g. `jq 'keys'`).
4. Run one card per lane; record `usage.cache_read_input_tokens` (Anthropic),
   `input_tokens_details.cached_tokens` (OpenAI), or
   `X-OpenRouter-Cache-Status` (OpenRouter) from provider logs/console.
5. Inventory which n8n workflows call LLMs, and with which key.
6. **R1b:** one live `messages.create` against `claude-sonnet-5` with the
   router's exact parameters. If it 400s, fixing `extract_claude` (drop
   `temperature`, select the `text` block by type, raise `max_tokens` or set
   `thinking: {type: "disabled"}`) is **blocking**: the memory loop is down.
   Outranks every caching item.

Decision thresholds: R4 applies only if some profile is on OpenRouter. R6 only
if the median orchestrator resume gap is 5–60 min *and* a resume turn shows
`cache_read_input_tokens` covering the system prompt. If a lane shows zero
cache reads on turn 2+, investigate its provider route (§3a) before anything
else.

### R2. Make `/traces` idempotent on `trace_id`

- **What:** if the `ReasoningTrace` already exists with
  `extraction_status = 'extracted'`, return the stored strategy without calling
  the backend or touching counters.
- **Where:** `services/hermes-memory-router/main.py` `ingest_trace`; tests first.
- **Benefit:** retries stop skewing `success_rate`; also saves the repeat call.
- **Verify:** post the same `trace_id` twice; counts unchanged, zero backend calls.

### R3 (M1). Separate provider keys, with spend caps, per service

- **What:** distinct Anthropic (or OpenRouter) keys/workspaces for Hermes,
  hermes-memory-router and n8n, each with a spend limit.
- **Where:** provider console + Railway env; no code change.
- **Benefit:** per-service attribution, budgets, and per-key cache metrics
  without a gateway. It also bounds a leaked key.
- **Limit:** per-profile attribution inside Hermes is not real, since lanes can
  read each other's env (SECURITY.md gap 1).
- **Verify:** usage appears under the expected key/workspace; cap enforced.

### R4. Set `openrouter.response_cache` explicitly per profile

- **What:** only if R1 shows OpenRouter. Write the value in each profile's YAML
  (repo-visible) rather than `HERMES_OPENROUTER_CACHE` (Railway-only).
  Suggested: `false` for worker lanes; a conscious choice for `default`.

  ```yaml
  openrouter:
    response_cache: false
  ```

- **Where:** `services/hermes/profiles/<name>/config.yaml`.
- **Why:** make hidden, TTL-bounded state explicit and reviewable. Expected cost
  impact negligible.
- **Risk:** per-profile override behaviour is read from the loader, not tested.
- **Verify:** no `X-OpenRouter-Cache-Status` on that profile's traffic.

### R5. Memory-router consistency first; optional keyed skip later

- **Step 1 (do):** have the `MERGE` return the stored `s.title` /
  `s.description`, and use *those* for the Qdrant embedding and the API
  response. Fixes §5.3 with no cache semantics.
- **Step 2 (optional, after measuring the duplicate rate):** skip extraction
  only when the stored node matches on
  `(strategy_id, backend, model, prompt_version)`, recorded on the
  `StrategyItem`. `outcome` is in the prompt but not in `strategy_id`, so decide
  explicitly whether it belongs in the key. Add an explicit re-extract /
  quarantine path (needed anyway given §5.4). This is first-writer-wins:
  stable, but order-dependent — not "reproducible from inputs".
- **CI guard:** `tests/integration/test_hermes_memory_router.py` posts fixed
  `raw_reasoning` strings with fresh `trace_id`s to a long-lived staging router
  with a funded key (`.github/workflows/ci.yml` integration job). A naive skip
  would make CI stop exercising real Claude extraction. Salt the inputs, or add
  an explicit skip-path test, before step 2.
- **Verify:** Qdrant payload title == Neo4j title == response title after a
  duplicate post.

### R6. 1 h prompt-cache TTL for the orchestrator: deferred

With `cache_ttl: "1h"`, every breakpoint write is at 2×, including the tail
breakpoints written every turn, not just the system prefix. The draft's
break-even (one resume per hour) ignored that. It is also unverified that a
kanban resume reuses a byte-identical system prompt. Revisit only on the R1
thresholds.

### R7. No gateway response cache, no semantic cache, Uzora stays MCP-only

For Hermes, OpenCode, and Uzora, per §2 and §7. Revisit triggers: an LLM eval
or CI harness replaying fixed prompts appears (then consider OpenRouter's
existing cache or a Braintrust self-hosted plane, `never` on anything
stateful); or the SECURITY.md gap 2 egress proxy is built (then evaluate
credential brokering there, as its own service).

## 9. Open questions / assumptions

- Which provider/model each Hermes profile and OpenCode lane uses (R1).
- Whether any Locus traffic is byte-identical (e.g. n8n calling the Hermes API
  on 8642 with fixed prompts). Assumed no.
- Orchestrator resume-gap distribution; duplicate-trace rate.
- Whether `outcome` should change a strategy's extraction key (R5 step 2).
- Assumed Uzora never proxies model traffic.

## 10. What this research did NOT verify

- No live calls to any provider, gateway, or the Railway deployment. Cache
  behaviour, hit rates, savings, and the `/traces` 502 are inferred.
- Hermes behaviour is from `v2026.8.16` source and docs, not a running
  instance, including per-profile `openrouter:` overrides and whether a kanban
  resume keeps a byte-identical system prompt.
- OpenCode behaviour is from `v1.18.32` source; whether its `openaiCompatible`
  `cache_control` survives an OpenAI-compatible proxy to Anthropic.
- Whether Braintrust forwards `cache_control` through `/chat/completions`, and
  whether `/v1/messages` is response-cached (inferred not).
- Braintrust's claims that the cache is key-holder-only and "Braintrust cannot
  see your data".
- Whether Ollama reuses a KV prefix across separate `/api/generate` calls.
- Hermes's "~75%" input-cost reduction figure.
- Anything on `feat/uzora-mcp-gateway` beyond `main.py`, `README.md`,
  `auth.py`.
