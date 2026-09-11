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

For a backend that requires authentication, set `api_key_env` to the name of
an environment variable. InferenceDock reads that variable for upstream,
lifecycle, and state-probe requests and sends a Bearer header; do not put the
key itself in `engines.yaml`, Agent configuration, or logs. Agent-to-dispatcher
credentials remain independent from dispatcher-to-backend credentials.

The Agent helper exposes a small fixed candidate list for Pi, Hermes, OpenCode,
OpenCodex, ZCode, and DSH. It checks only those known files; selecting another
file must be explicit and no directory-wide scan is performed. This release
uses environment-variable references for backend keys; it does not invoke the
macOS Keychain or read secret output.

An externally owned service may expose `active_requests_path` and
`model_state_path`. The first must return `{"active_requests": N}`; the second
must return a reliable resident-model list (`data`, `models`, or
`loaded_models`) or `{"loaded": true|false}`. These are lifecycle probes, not
ordinary catalog endpoints. Without an activity probe, InferenceDock refuses
to unload an externally owned model because direct requests cannot be ruled
out.

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

Model availability can use three explicit, read-only evidence sources:

- `adapters.<id>.catalog_args` invokes `command[0]` with a native JSON listing,
  such as MTPLX's `models --json`.
- `adapters.<id>.models_path` reads the model list only when that service is
  already healthy; declare it only for a complete local catalog, not an API
  that reports just the currently loaded model. It never starts a stopped
  backend.
- `models.<id>.asset_paths` checks only the listed files. Relative paths are
  resolved beside the configuration file, and no containing directory is
  scanned.

Use `models.<id>.catalog_id` when the native catalog ID differs from the API
`backend_model`. Catalog responses may use a root `data` or `models` list and
the entry fields `repo_id`, `id`, `name`, or `model`.

Use `models.<id>.runtime_model_id` when a server reports live settings under a
loaded model ID that differs from the request or catalog ID. InferenceDock then
reads context and runtime flags from that server entry without reading GUI files.

Probes are cached for at most 10 seconds. A confirmed missing catalog entry or
asset marks the model unavailable, omits it from `/v1/models`, and rejects a
new managed start. Unreachable services and invalid probe responses remain
`unknown`, so a temporary failure cannot erase the runtime menu. `/v1/status`
retains the configured row and sanitized evidence for diagnosis; no local path
is returned. Restoring the native model makes the configured entry available
again without rewriting YAML.

## Reviewed removal of configuration records

Unavailable rows stay in the configuration on purpose. Removing them is an
explicit, reviewed action (`/v1/prune-models`, or Models tab in the menu) that
keeps the same fail-closed rules as the rest of the dispatcher:

- The request must carry the exact model IDs, the exact unused-adapter IDs, and
  the current `revision` from `GET /v1/config`. A stale revision, a missing or
  duplicate ID, and an empty selection are all rejected with 400/409.
- A model is removable only while it is confirmed missing locally (catalog,
  `models_path`, or `asset_paths`) and not resident. A model that is loading,
  ready, unknown, or referenced as another model's `canonical` target is
  refused, and the last configured model can never be removed.
- An adapter is removable only when no remaining model references it and no
  live process owned by the dispatcher is running it.
- The previous file is copied to `<config>.bak-before-prune-<ns>` with `0600`
  permissions before the replacement, the write is atomic, and a failure
  restores the backup. Model files, third-party configuration, and provider
  files are never touched.
- Policy entries for IDs that no longer exist are dropped from the settings
  file at the same time, so the drift warning does not reappear on every
  restart. That file is backed up as `<settings>.bak-before-prune-<ns>` with
  `0600` permissions first, and a failure there is reported separately instead
  of rolling the configuration back.

`GET /v1/config` also reports `settings_path`, `settings_warnings`,
`unused_adapters`, and the `revision` used above, so an operator can see why a
runtime decision was taken.

## Policy file drift

`settings.json` outlives `engines.yaml`. If it still lists a model or adapter
that was removed from the configuration, those IDs are ignored on load and
reported as a warning (`settings_warnings`) instead of invalidating the file.
Smart scheduling, the idle-unload timeout, and the surviving per-model policies
stay active. Policy writes through `POST /v1/settings` are still strict: an
unknown ID in a request body is rejected so a typo cannot create a policy for a
model that does not exist. `POST /v1/reload` keeps the effective policy across
a configuration reload and starts the idle timer if the reload enables one. A
reviewed cleanup persists the filtered result, so the warning clears itself
instead of repeating on every restart.

## Metrics configuration

Set `metrics_path` to a dedicated, writable JSONL path outside the application
bundle. The optional `/v1/metrics?limit=N` endpoint returns recent request rows
and per-model summaries. Summaries count only explicit successful finishes
(`stop`, `tool_calls`, `length`, `eos`, `complete`) with positive backend rates;
disconnects, cancellation, missing finishes, and zero/negative rates are
excluded and remain visible in `excluded_requests`. A max rate without `n` and
the window is not a benchmark. Prompts, images, tool payloads and credentials
must never be copied into metrics.
