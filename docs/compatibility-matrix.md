# Compatibility Matrix

This matrix describes the **Developer Preview** contract shipped by the
repository. `real tested` is intentionally separate from registration: a
manifest or endpoint declaration is not proof that an engine can load a model,
stream, cancel, unload, or survive a long context.

| Integration | Discover | Configure | Manage | Real tested | Notes |
| --- | --- | --- | --- | --- | --- |
| MLX-Serve | manifest/explicit endpoint | explicit endpoint + model ID | `observe`; native routes only after probe | **Local dated run; none in CI** | First-class path; MLX owns inference, batching, concurrency, KV and memory. |
| DS4 Router / DS4f | manifest/runtime preview | generated config after reviewed fixture | `observe` during migration; `managed` after takeover | **Local dated run; fixture in CI** | Existing Router may own port 8888; takeover never silently stops it. |
| MTPLX | manifest/executable probe | explicit command, assets and port | `managed` when all are present | **No** | Missing command/assets remains unavailable; refresh never cold-starts a real model. |
| llama.cpp server | not bundled | manual endpoint/model mapping | `observe` | **No** | Use native router/load semantics only after fixed-version verification. |
| Ollama | manifest endpoint probe | manual endpoint/model mapping | `observe` | **No** | `/api/ps` is required to prove residency; `/v1/models` alone is insufficient. |
| oMLX | manifest/explicit endpoint | explicit endpoint + model ID | `observe` | **No** | RAG service may stay resident and outside exclusive control; memory still matters. |
| LM Studio | manifest/explicit endpoint | explicit endpoint + model ID | `observe` or `http-managed` after route probe | **No** | Native load/unload route and effective config must be verified per version. |
| mlx-lm | executable/config probe | manual command and model mapping | `managed` only with reviewed argv | **No** | CLI capability is not inferred from a filename. |
| mlx-vlm | executable/config probe | manual model + vision components | `managed` only with reviewed argv | **No** | Vision requires weights, projector, template and image request proof. |
| vllm-mlx | explicit endpoint | manual endpoint/model mapping | `observe` | **No** | Linux/CUDA-style process management is outside this macOS-first preview. |
| Fake backend | repository fixture | generated test config | `managed` | **Yes (deterministic)** | Used by smoke and acceptance checks only; it is not inference quality evidence. |

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
