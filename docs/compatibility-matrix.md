# Compatibility Matrix

This matrix describes the **Developer Preview** contract shipped by the
repository. `real tested` is intentionally separate from registration: a
manifest or endpoint declaration is not proof that an engine can load a model,
stream, cancel, unload, or survive a long context.

| Integration | Discover | Configure | Manage | Real tested | Notes |
| --- | --- | --- | --- | --- | --- |
| MLX-Serve | manifest/explicit endpoint | explicit endpoint + model ID | `observe`; native routes only after probe | **Local dated run; none in CI** | First-class path; MLX owns inference, batching, concurrency, KV and memory. |
| FastMLX | manifest/executable + endpoint probe | explicit endpoint + discovered model ID | `observe`; dynamic routes require fixed-version proof | **No** | OpenAI model catalog is declared; query-parameter load/unload semantics are not yet managed. For large models, configure one worker explicitly and never use development `--reload` in production. |
| DS4 Router / DS4f | manifest/runtime preview | generated config after reviewed fixture | `observe` during migration; `managed` after takeover | **Local dated run; fixture in CI** | Existing Router may own port 8888; takeover never silently stops it. |
| MTPLX | manifest/executable probe | explicit command, assets and port | `managed` when all are present | **No** | Missing command/assets remains unavailable; refresh never cold-starts a real model. |
| llama.cpp server | manifest/executable probe | explicit argv, endpoint and model | single-model `managed`; router routes require version proof | **No** | No native unload claim without a verified router API. |
| Ollama | manifest/API probe | explicit endpoint and model ID | native API with verified `/api/ps` residency probe | **No** | Catalog and resident-model state remain separate. |
| oMLX | manifest/explicit endpoint | explicit endpoint + model ID | `observe` | **No** | RAG service may stay resident and outside exclusive control; memory still matters. |
| LM Studio | manifest/explicit endpoint | explicit endpoint + model ID | `observe` or `http-managed` after route probe | **No** | Native load/unload route and effective config must be verified per version. |
| mlx-lm | executable/config probe | manual command and model mapping | `managed` only with reviewed argv | **No** | CLI capability is not inferred from a filename. |
| mlx-vlm | executable/config probe | manual model + vision components | `managed` only with reviewed argv | **No** | Vision requires weights, projector, template and image request proof. |
| vllm-mlx | executable/config candidate | explicit argv, endpoint and model | `managed` only with reviewed argv | **No** | Conservative template; no lifecycle claim until a fixed install is verified. |
| Fake backend | repository fixture | generated test config | `managed` | **Yes (deterministic)** | Used by smoke and acceptance checks only; it is not inference quality evidence. |

## Runtime safety notes

- A backend state probe that times out or returns an invalid shape is exposed as
  `unknown` with its last confirmed state and timestamp. Unknown state blocks
  eviction and model replacement until a probe confirms it.
- Adapter credentials use an environment-variable reference (`api_key_env`)
  and are added to backend inference, lifecycle, and state-probe requests.
  Agent credentials and backend credentials are separate; values never enter
  request logs or metric files.
- MLX-Serve has one narrowly scoped recovery path: a managed process owned by
  InferenceDock may be restarted once for an explicit Metal GPU timeout before
  any streaming bytes are sent. Other HTTP errors are passed through.
- `/v1/metrics` keeps the recent request tail plus a small persistent numeric
  aggregate by model and cold/cache bucket. Prompt, image, tool and credential
  fields are discarded before persistence.
- Discovery and model APIs expose separate `platform`, `installation`, and
  `instance` identities. A scan/import is still a candidate until explicitly
  configured; duplicate backend model IDs within one instance are represented
  once with aliases.
- Plugin scans retain the legacy `endpoint_running` field as a reachability
  signal. Use `endpoint_reachable` and `identity_verified` together: an HTTP
  404/500 means only that the port answered and is reported as
  `reachable_unverified`, never as a confirmed engine.
- Successful loads may record one post-load listener RSS sample as
  `observed_memory_*` evidence. It is audit data only and never overwrites the
  configured estimate automatically.

## Compatibility levels

- `test`: covered by deterministic tests without a real model.
- `managed`: InferenceDock can start/stop the process described by the manifest and owns the lifecycle.
- `observe`: InferenceDock probes and routes to an existing service but does not control it.
- `draft`: manifest shape is accepted, but a command, asset, or runtime proof is still missing.

For each integration, `discover`, `configure`, `manage`, and `real tested` are
independent facts. A future release should replace **No** only with a dated
run manifest containing the fixed upstream version, binary fingerprint, model
artifact, request cases, and rollback evidence.

The dated local observations are documented in
[`runtime-validation-2026-09-10.md`](runtime-validation-2026-09-10.md). They are
operator evidence with stated limits, not CI certification or a long-context guarantee.

The Agent endpoint is `http://127.0.0.1:18800/v1`. Existing clients that point directly at 11234, 8000, 8001, or another vendor port remain compatible when their provider configuration is left unchanged; they are not automatically redirected.
