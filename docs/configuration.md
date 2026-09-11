# Configuration Guide

## Public and private layers

`config/engines.example.yaml` is the only implicit input to a public package. It contains loopback endpoints and placeholders. A private package must opt in explicitly:

```sh
MODEL_DISPATCH_PUBLIC=0 \
MODEL_DISPATCH_CONFIG="$HOME/Library/Application Support/InferenceDock/engines.yaml" \
  sh scripts/package_app.sh
```

The public package contains only an example configuration. The menu companion
prefers the user-owned `~/Library/Application Support/InferenceDock/engines.yaml`
when present and falls back to the bundled example only when that file is
absent. Keep the user configuration outside the source archive; the menu shows
the selected path in model-management diagnostics.

Each adapter declares an endpoint or a managed command. Models belong to an adapter and may set `resource_group`, `exclusive_groups`, `keep_resident`, `estimated_memory_gb`, capabilities, aliases, and a context window. The effective configuration is the union of public manifests and the selected config; user policy changes are written through `/v1/settings`. The menu's model details must show desired/configured values separately from observed/effective values; `unknown` means the backend did not expose a reliable value.

`smart_scheduling` controls automatic replacement: when disabled, an API request
may use an already-loaded model or load a model that needs no eviction, but it
cannot evict a resource-group occupant. Use the explicit `/v1/switch` endpoint
for a reviewed manual replacement. Memory-pressure admission remains enforced
in both modes.

### MLX-Serve first

Start with an `external` adapter for an already-running MLX-Serve instance:

```yaml
adapters:
  mlx-serve:
    type: external
    endpoint: http://127.0.0.1:11234
    health_path: /health
    models_path: /v1/models

models:
  qwen38-27b:
    adapter: mlx-serve
    backend_model: qwen38-27b
    context_window: 262144
    max_output_tokens: 4096
    reasoning_levels: [off, high, max]
    capabilities: {chat: true, stream: true, vision: false}
```

Do not add `activate_path` or `deactivate_path` from a README alone. Probe the
fixed MLX version, confirm response semantics and a rollback, then record the
route in a reviewed config. Keep the MLX server outside dispatcher ownership
until that evidence exists.

## Migration pattern

1. Back up the current Agent/provider configuration.
2. Add one adapter at a time, preserving its existing endpoint.
3. Route a canary client to `http://127.0.0.1:18800/v1` and select a canonical model ID.
4. Keep the old endpoint available until health, streaming, cancellation, and unload checks pass.
5. Roll back by restoring the backup and stopping only processes that InferenceDock owns.

Discovery (`plugin_registry scan` and `/v1/reconcile`) is read-only. It reports candidates but does not download weights, rewrite provider files, or terminate an external server.

## Metrics configuration

Set `metrics_path` to a dedicated, writable JSONL path outside the application
bundle. The optional `/v1/metrics?limit=N` endpoint returns recent request rows
and per-model summaries. Summaries count only explicit successful finishes
(`stop`, `tool_calls`, `length`, `eos`, `complete`) with positive backend rates;
disconnects, cancellation, missing finishes, and zero/negative rates are
excluded and remain visible in `excluded_requests`. A max rate without `n` and
the window is not a benchmark. Prompts, images, tool payloads and credentials
must never be copied into metrics.
