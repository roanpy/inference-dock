# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
There is no numbered public release yet; the repository is an experimental
developer preview.

## [Unreleased]

### Fixed

- A settings file that still referenced a deleted model or adapter no longer
  discards the whole saved policy. Unknown policy IDs are ignored with a visible
  warning, so smart scheduling and the idle-unload timeout survive a cleanup.
- Settings pages open at their first row and keep content at the top instead of
  showing blank space above a stretched page.

### Changed

- Cleaning missing mappings is now a reviewed operation: the menu sends the
  exact model and unused-adapter IDs plus the configuration revision, and the
  core refuses a stale revision, a resident model, or a still-referenced
  adapter. A permission-restricted backup of the previous file is always kept.
- The diagnostics page shows the configuration and policy file paths, the
  configuration revision, active policy, ignored policy IDs, and unused
  adapters.
- Run history also renders the stored cross-restart aggregates with their
  sample counts; it is no longer limited to this process's in-memory tail.
- The import template list is read from the bundled plugin manifests, so a new
  engine manifest appears without editing the menu source.
- Cleaning missing mappings also drops the now-inert policy entries for IDs that
  no longer exist, so the drift warning clears instead of repeating on every
  restart; the settings file is backed up with `0600` permissions first.

### v0.1.0-dev.2

- Menu server groups now use API/config adapter IDs directly, with localized
  state labels and color indicators.
- Model management exposes read-only discovery candidates, source adapters, and
  an explicit import-preview entry; configured, reported, and verified
  capability evidence remains distinct.
- The menu prefers the user Application Support configuration and reports the
  selected path; public bundles continue to ship only the example config.

### Added

- Unified model availability evidence from native CLI catalogs, healthy HTTP
  model lists, and explicitly configured asset paths. Missing models leave the
  runtime API while configuration and sanitized diagnostics remain available.
- Loopback OpenAI-compatible dispatcher with explicit public-to-backend model
  routing.
- `fake`, `managed`, and `external` adapter boundaries with a single active
  resource group in the MVP.
- Declarative plugin registry commands: `check`, `scan`, and `preview`.
- Declarative `keep_resident`, `exclusive_group`, and opt-in idle unload
  controls with an observe-only adapter mode.
- Request metrics for usage, TTFT, cache tokens, SSE completion, disconnects,
  and backend-reported timing fields.
- macOS menu bar companion and local packaging scripts.
- Canonical server-grouped menu rows with state color, cancellation, runtime
  details, run history, and native collapsed diagnostics.
- Deterministic public packaging and source-export scans that exclude private
  configs, credentials, logs, and real model artifacts.
- English-first compatibility, configuration, troubleshooting, acceptance, and
  nightly-runbook documentation.
- MLX-first setup guide, Chinese operator guide, security/contribution policy,
  and third-party licensing boundary.
- Menu history reads `/v1/metrics` when available and shows cold-load, queue,
  TTFT, prefill, decode, cache, max/mean/median, sample and exclusion counts.

### Fixed

- Hide unavailable unloaded models from the runtime menu and Agent preview,
  while retaining loaded rows for safe unload and settings rows for recovery.
- Reject switches that would require stopping or releasing an external service.
- Reject external unload requests when the dispatcher does not own the service.
- Check managed endpoint-port availability before starting a child process and
  clean up the owned process group after startup failure.
- Require matching `endpoint` and `port` values when both are configured.
- Re-check dispatcher state after waiting for active requests before switching.
- Record client-disconnected streams without fabricating a successful finish.
- Measure TTFT only from a non-empty content, reasoning, or tool-call delta.
- Do not label locally estimated decode speed as a backend-reported metric.
- Re-check idle state under the dispatcher condition before unloading and never
  unload observe-only services.
- Distinguish loaded, generating, unloaded, failed, unknown, and external-owned
  states in menu labels; never treat `busy` as a load state.

### Limitations

- No real-model end-to-end certification is included in this preview.
- External services remain outside lifecycle ownership and global memory
  accounting.
- The source exporter and release archive still require maintainer review before
  publication.
