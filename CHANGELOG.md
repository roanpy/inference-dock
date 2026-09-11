# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
There is no numbered public release yet; the repository is an experimental
developer preview.

## [Unreleased]

### v0.1.0-dev.2

- Menu server groups now use API/config adapter IDs directly, with localized
  state labels and color indicators.
- Model management exposes read-only discovery candidates, source adapters, and
  an explicit import-preview entry; configured, reported, and verified
  capability evidence remains distinct.
- The menu prefers the user Application Support configuration and reports the
  selected path; public bundles continue to ship only the example config.

### Added

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
