<div align="center">

# InferenceDock

**One local API for open-source model runtimes on macOS.**

Coordinate ports, processes, memory handoffs, and Agent configuration without patching engine source.

**English · [简体中文](README.zh-CN.md)**

[![Status: Developer Preview](https://img.shields.io/badge/status-developer_preview-2563eb)](docs/release-notes.md)
[![CI](https://github.com/roanpy/inference-dock/actions/workflows/ci.yml/badge.svg)](https://github.com/roanpy/inference-dock/actions/workflows/ci.yml)
![macOS 13+](https://img.shields.io/badge/macOS-13%2B-111827?logo=apple)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/license-MIT-16a34a.svg)](LICENSE)

</div>

## What it does

InferenceDock is a small, loopback-only model-service dispatcher with an
optional macOS menu bar companion. It gives local agents one OpenAI-compatible
entry point while keeping inference engines, model files, and runtime versions
under the operator's control.

> **Status: v0.1.0-dev.2 developer preview.** Lifecycle behavior is covered by isolated
> tests, while real-engine compatibility still depends on each local install.
> Discovery does not certify model quality, context-window safety, or production
> readiness.
> See the [release notes](docs/release-notes.md) and [changelog](CHANGELOG.md)
> for the shipped surface, and the [runtime validation report](docs/runtime-validation-2026-09-10.md)
> for measured hardware evidence and explicit test limits.

The 2026-09-10 upstream snapshot is recorded in the [release notes](docs/release-notes.md):
DS4 and Qwen PR991 remain conflict-sensitive, while MLX-Serve `v26.9.2` and
MTPLX `v2.11.2` match the latest local checks. These are isolated metadata and
contract checks only; no third-party source is modified.

## What this solves

Local model setups often grow into several ports, launch scripts, resident
large models, and provider files that drift independently. InferenceDock adds
one small coordination layer for those handoffs: it knows which instance owns
which model, refuses unsafe switches, and leaves inference, batching and cache
policy to the engine that already implements them.

InferenceDock gives local agents one stable `127.0.0.1:18800/v1` endpoint while
MLX-Serve, DS4, and MTPLX remain independently configurable. The menu groups
servers directly by the configured/API adapter ID, exposes one canonical row per backend model, and makes ownership
boundaries visible instead of silently taking over a running service.

### MLX-first configuration

The shortest useful path is an existing MLX-Serve endpoint. Register the
loopback service and the backend model ID; InferenceDock probes and forwards,
while MLX keeps inference, batching, concurrency, KV cache, and model memory
ownership:

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
    capabilities: {chat: true, stream: true, vision: false}
```

`external` is intentionally conservative: an unreachable service is not a
missing model, and a declaration is not a capability test. Add native MLX
load/unload routes only after a fixed-version probe and a documented rollback.
The [Chinese guide](README.zh-CN.md) contains a copyable migration checklist.

| First-class path | Status | Safe claim |
| --- | --- | --- |
| MLX-Serve | `observe` | Probe and route to an existing OpenAI-compatible endpoint |
| DS4 / DS4f | `observe` or `managed` | Keep an old Router during migration; takeover is explicit |
| MTPLX | `draft` or `managed` | Requires an explicit command and verified assets |

Read the [compatibility matrix](docs/compatibility-matrix.md),
[configuration guide](docs/configuration.md), and [acceptance checklist](docs/acceptance-checklist.md)
before calling this a replacement for an existing installation.

## What is verified

- Public model IDs are mapped explicitly to backend model IDs and adapters.
- `fake` adapters run an isolated loopback backend for repeatable tests.
- `managed` adapters start explicit argv processes owned by this dispatcher.
- `http-managed` adapters call model load/unload routes on an already-running
  local engine.
- `external` adapters proxy existing loopback services but never stop or unload
  them. A switch that would require releasing an external service is rejected.
- `observe` adapters are visible for status only and are never controlled.
- Same-model requests are forwarded concurrently to the third-party backend;
  InferenceDock does not add a second queue. Resource-group capacity governs
  cross-model admission, and an active request is allowed to finish before a
  managed switch.
- Chat Completions and Responses API requests share the same scheduling path.
  Unsupported backend routes preserve the backend error instead of being emulated.
- Streaming responses are forwarded as SSE. Disconnects close the upstream and
  are recorded as `client_disconnect`; incomplete streams are not relabeled as
  successful stops.
- The recorder stores timing, usage, and backend-reported rates without request
  prompts, images, tool results, or credentials.
- The menu shows configured capabilities separately from backend-reported and
  runtime-verified evidence; unavailable evidence remains `unknown`.
- Model discovery is read-only. The menu lists candidates and source adapters,
  while configuration import stays an explicit reviewed operator action.
- Configured CLI catalogs, healthy HTTP model lists, and explicit asset paths
  can mark a local model missing. It then leaves `/v1/models`, the runtime menu,
  and Agent previews while its diagnostic/configuration record is retained.
- Agent configuration changes use the version-aware `scripts/agent_config.py`
  preview/validate/apply/restore flow with sibling backups; the menu companion
  validates and previews but does not silently rewrite provider files.
- Registry results keep platform, installation, and instance identities
  separate; duplicate backend model IDs within one instance collapse to one
  canonical entry with aliases.
- A successful load can contribute one post-load RSS observation for review.
  It never silently replaces the configured memory estimate.

Multiple resource groups are supported; capacity `1` is recommended for large
models that share unified memory. Services can remain outside an exclusive
group when they are intentionally resident.

## Quick start

Requirements: Python 3.11+, [PyYAML](requirements.txt), and (for the menu app)
macOS 13+ with Swift 5.9+.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/model_dispatch.py \
  --config config/engines.example.yaml --check-config
.venv/bin/python scripts/model_dispatch.py \
  --config config/engines.example.yaml
```

The example starts no real model. It launches the fake backend only when a
request or switch targets `fake-alpha` or `fake-beta`, and listens on
`http://127.0.0.1:18800`.

```sh
curl http://127.0.0.1:18800/v1/models
curl http://127.0.0.1:18800/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"fake-alpha","messages":[{"role":"user","content":"Hello"}]}'
```

Use `--check-config --probe` for read-only health checks of managed or external
adapters. The command never starts, stops, or switches a backend during a
config check.

## Configuration and ownership

Configuration separates adapters, public models, and resource groups. Adapter
commands are argv arrays (or shell-split command strings for compatibility); no
shell is invoked by the dispatcher. Endpoints are restricted to loopback HTTP.
When both `endpoint` and `port` are provided, they must match.

| Adapter | Dispatcher action | Ownership boundary |
| --- | --- | --- |
| `fake` | Start/stop test backend | Owned by this process |
| `managed` | Start/stop configured argv process | Must use `lifecycle_owner: model-dispatch` |
| `http-managed` | Call configured load/unload HTTP routes | Engine process remains externally owned |
| `external` | Health check and proxy only | Existing service remains running |
| `observe` | Status only | No lifecycle or request ownership |

Managed startup first checks that the endpoint port is free. A healthy service
already listening there is not accepted as proof that the new child process
started. Startup failures clean up the owned process group.

External services are not forcibly reclaimed. Switching away from an external
adapter, switching between different external adapters, or unloading an
external model returns a conflict when release cannot be proven. Direct calls
to other servers remain outside this dispatcher's ownership and accounting.

## Plugin registry

`scripts/plugin_registry.py` reads declarative manifests and performs bounded,
read-only discovery. Unknown manifests are reported as errors; they are not
executed. Built-in manifests may opt into a version probe. Config readers only
parse bounded JSON, YAML, TOML, or plist data and redact secret-like fields.
`scripts/plugin_runtime.py preview` can compile that discovery into a runtime
configuration preview; it never writes or activates the result. Unresolved
managed commands remain visibly unresolved instead of being guessed.

The DS4 takeover command reads its existing runtime JSON and launchd plists,
then emits managed adapter configuration without editing DS4 source. The update
monitor compares local Git heads with `upstream` when present, otherwise
`origin`; it never fetches, checks out, merges, or pushes.

`scripts/migration_helper.py` can prepare a read-only file migration plan from
legacy loopback endpoints and default-model variables to a validated target
configuration. Applying it requires `--apply`, explicit file arguments, and a
backup for every changed file. See [docs/migration.md](docs/migration.md).

```sh
python3 scripts/plugin_registry.py check \
  --plugins-dir plugins
python3 scripts/plugin_registry.py scan \
  --plugins-dir plugins --root "$HOME/Developer"
python3 scripts/plugin_registry.py preview \
  --plugins-dir plugins --path mtplx=$HOME/.mtplx/bin/mtplx
python3 scripts/plugin_runtime.py preview --plugins-dir plugins
python3 scripts/update_monitor.py --plugins-dir plugins --pretty
```

`--path ID=EXECUTABLE` is an explicit executable override. Discovery does not
infer compatibility from a filename, execute repository scripts, source shell
profiles, download models, or write host configuration.

## Metrics and logs

Request metrics are written to `logs/request-metrics.jsonl` by the example
configuration (or to the configured path) and rotate at 10 MiB. The optional
`GET /v1/metrics?limit=N` endpoint returns recent requests and per-model
summaries. TTFT is
measured at the first non-empty content, reasoning, or tool-call delta. Decode
and prefill rates are reported only when the backend supplies those timings;
missing values remain absent/unknown. Calls made directly to an engine are not
included in dispatcher metrics, and backend-owned logs may contain their own
output. The menu's run-history surface keeps cold-load, queue, TTFT, prefill,
decode, cache-token, finish, and error fields distinct; missing values are
shown as `unknown`. Max/average/median rates must include their sample count
and window, so a one-request maximum is never presented as a benchmark. Only
explicitly completed finishes (`stop`, `tool_calls`, `length`, `eos`, or
`complete`) with positive rates enter the speed aggregates; cancellation,
disconnects, missing finishes, and zero/negative rates are counted as excluded.

## Tests and local build

Run the dependency-light smoke suite:

```sh
python3 -m py_compile scripts/fake_backend.py scripts/model_dispatch.py \
  scripts/plugin_registry.py tests/run_tests.py
python3 tests/run_tests.py
```

The suite covers unknown-model rejection, content-type validation, managed
switch waiting, same-model coalescing, independent resource-group concurrency,
SSE completion and disconnect cleanup, timeout/cancel paths, external ownership
conflicts, managed port conflicts, startup failure cleanup, endpoint/port
validation, discovery/config validation, and public packaging scans. Build the
menu companion with:

```sh
swift build
```

`swift build` is a compile check. Use `sh scripts/package_app.sh` to produce the
localized `.app` bundle; the standalone SwiftPM executable does not bundle the
menu resources.

The app is a local developer build, not a signed or notarized installer. It
uses the dispatcher API and does not add lifecycle ownership for external
services. The menu is bilingual: macOS Simplified Chinese (`zh-Hans`/`zh-CN`)
uses the bundled Chinese strings, while every other preferred language uses the
English fallback. Language follows the system setting; there is no second app
language switch to drift out of sync. See [README.zh-CN.md](README.zh-CN.md).
At launch the menu uses `~/Library/Application Support/InferenceDock/engines.yaml`
when it exists and otherwise uses the bundled public example; the selected path
is visible in model-management diagnostics.
Quitting the menu bar leaves the dispatcher core and active requests running;
use the explicit **Stop InferenceDock core** action to stop only a core process
started by this app. External services are never stopped by that action.
The native Settings window keeps the menu focused on runtime actions. It offers
server/config path checks, read-only instance and capability previews, policy
controls, agent endpoint previews, and update diagnostics. The Server tab can
preview an explicitly selected loopback endpoint, executable, source
directory, or supported config file through the bundled plugin importer; this
is deliberately read-only and never scans or writes `engines.yaml`. Deleting
import mappings, provider-file application, and release rollback remain disabled
until their backup-protected APIs are implemented.

Run the full local gate (it never starts a real model):

```sh
sh scripts/acceptance.sh
```

## Source archive

The maintainer release helper builds a reviewed, Git-history-free source
archive from an explicit allowlist:

```sh
python3 scripts/export_source.py dist/inference-dock-source.tar.gz
```

Inspect the archive before publishing. Keep host-specific configuration,
runtime files, credentials, and generated build output out of a public release.
See [troubleshooting and rollback](docs/troubleshooting.md) and the
[nightly real-engine runbook](docs/nightly-runbook.md) for operator-only tests.

## Scope and limitations

- No model downloader, inference implementation, engine updater, or automatic
  parameter tuner.
- No global memory controller. Resource capacity `1` is a serialization rule,
  not a proof of available RAM or GPU memory.
- No automatic Git checkout, merge, launchd installation, or unattended update.
- DS4 takeover only generates reviewed runtime configuration; applying it is an
  explicit local operator action.
- Real DS4, MLX-Serve, and MTPLX end-to-end inference is not claimed by the
  fake-backend test suite. Use `config/local.preview.yaml` only after checking
  executable paths, API contracts, and lifecycle ownership on the target host.
- The dispatcher is loopback-only and has no authentication layer; do not bind
  it to a non-loopback interface.

## License

[MIT](LICENSE). PyYAML is a separately installed MIT dependency. Third-party
engines and model weights retain their own licenses; InferenceDock does not
redistribute them. Vendor names and protocol compatibility are descriptive,
not endorsements. Review each engine's upstream license before packaging it.
