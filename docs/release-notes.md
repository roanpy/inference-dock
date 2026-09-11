# InferenceDock Developer Preview

**Release status:** source-only experimental preview

**Date:** 2026-09-10
**Audience:** developers evaluating a local dispatcher boundary, not end users
running production inference.

## Highlights

- One loopback OpenAI-compatible endpoint for explicitly configured models.
- Separate lifecycle contracts for dispatcher-owned (`managed`) and
  observe-only (`external`) services.
- Resource-group capacity scheduling with active-request draining and memory admission.
- Explicit `keep_resident` and `exclusive_group` declarations plus opt-in idle
  unload for dispatcher-owned models.
- SSE forwarding with explicit completion and client-disconnect accounting.
- Declarative plugin discovery with bounded config readers and redacted fields.
- Optional Swift/AppKit menu bar companion for the local dispatcher API.

## Verification evidence

The repeatable test path uses only the repository's fake backend; it does not
download weights or start a real inference engine.

```sh
sh scripts/acceptance.sh
```

The smoke suite covers model allowlisting, request validation, active-stream
draining, SSE completion and disconnect cleanup, external ownership conflicts,
managed port conflicts and startup cleanup, endpoint/port consistency, and TTFT
content gating. A successful run is evidence for those local contracts only;
it is not a quality, latency, memory, vision, or long-context certification.

## Safety boundaries

- The server accepts loopback HTTP only and has no authentication layer.
- `external` services are never stopped or unloaded by this project. Switching
  away from one, switching between different external adapters, or unloading an
  external model is rejected when release cannot be proven.
- Managed startup refuses an occupied endpoint port rather than treating an
  unrelated healthy process as its own child.
- Prompts, images, tool results, and credentials are excluded from dispatcher
  request metrics. Backend-owned logs are outside that guarantee.
- Resource-group capacity `1` serializes dispatcher actions; it does not prove
  that a model fits available RAM or GPU memory.
- Idle unload is disabled unless `idle_unload_seconds` is configured; observe
  and external services are never unloaded by the idle worker.

## Plugin registry status

The registry command interface is available:

```sh
python3 scripts/plugin_registry.py check --plugins-dir plugins
python3 scripts/plugin_registry.py scan --plugins-dir plugins --root "$HOME/Developer"
python3 scripts/plugin_registry.py preview --plugins-dir plugins \
  --path ID=/absolute/path/to/executable
```

Manifests are declarative data. Discovery is bounded and read-only, and only
trusted built-in manifests may run an explicit version probe. This preview does
not claim that a third-party plugin or a real engine has passed lifecycle or
inference validation.

## Known limitations

- Resource groups can run independently; each group has an explicit capacity.
- No automatic service takeover, global memory controller, model downloader,
  engine updater, Git checkout/merge, or launchd installation is included.
- `config/local.preview.yaml` is a host-specific mapping and must be reviewed
  before use. Its executable paths and API contracts are not portable.
- The menu companion is an unsigned local developer build.

## Before a public release

Maintainers should review the source archive, dependency versions, license
notices, machine-specific paths, plugin manifests, and the target host's
external-service ownership before publishing a numbered release. Real DS4,
MLX-Serve, and MTPLX checks must be reported separately from this fake-backend
evidence.
