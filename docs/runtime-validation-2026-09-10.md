# Runtime validation: Apple M5 Max, 128 GB

Date: 2026-09-10

This report records real requests through InferenceDock's `127.0.0.1:18800`
OpenAI-compatible endpoint. Configuration declarations are not counted as
runtime support. A successful short request is not evidence of full context
stability.

## Reproducibility metadata

- Host: Apple M5 Max, 128 GB; macOS 26.6.2 (25G83).
- Dispatcher build lineage used for the first smoke: private commit `f3177c9`.
  The final dev.2 revalidation used `8cf1e8e` plus the documented UI-only
  follow-up; neither build modifies third-party inference source.
- Python 3.14.6; Apple Swift 6.2.4.
- DS4 main `1d38311e70f5`; DS4 Qwen runtime `62fa6c71a5cf`.
- MLX-Serve 26.9.2 with MLX 0.32.2.
- Redacted runtime configuration SHA-256:
  `660e00fa2eddd4ec8d35936fd3388c817032fedf6110655cc13d41f04782d1c3`.

The private runtime configuration and model paths are intentionally not
published. The figures below are operator observations, not a portable
benchmark; raw prompts and images were not retained in the public artifact.

## Result labels

- **Verified**: a real backend completed the named scenario through
  InferenceDock.
- **Configured only**: the adapter and model are declared, but no real request
  was completed in this run.
- **Not tested**: the scenario was intentionally skipped or exceeded this
  run's safety boundary.

## Verified engines and models

| Runtime | Model | Text | Vision | Cache evidence | Tested context | Decode evidence |
| --- | --- | --- | --- | --- | ---: | ---: |
| DS4 main | DeepSeek V4 Flash (`ds4high`) | Verified | Verified, one image | 4,096 cached of 4,509 prompt tokens | 4,509 | about 41.2 tok/s over 128 generated tokens |
| DS4 main | GLM 5.3 Flash Q2 | Verified | Verified, one image | 4,096 cached of 4,511 prompt tokens | 4,511 | about 31.5 tok/s over 140 generated tokens |
| DS4 PR991 runtime | Qwen 3.8 Flash Next | Verified | Verified, two images | 8,192 cached of 9,045 prompt tokens | 9,045 | about 52-55 tok/s |
| MLX-Serve 26.9.2 | Qwen 3.8 27B | Verified | Verified, one image | Native prefix cache available | Short request | short samples only |
| MLX-Serve 26.9.2 | Qwen 3.8 Flash Next | Verified | Verified, two images | 63,986 cached of 64,017 prompt tokens | 64,017 | about 63.7-75.3 tok/s in recorded samples |
| MTPLX 2.11.2 | Qwen 3.5 4B | Verified | Not tested (model declares text only) | RAM prefix hit observed | Short request | 92.1 tok/s for the successful 2-token sample |

The MLX Flash cold 9,018-token prefill measured about 1,521 tok/s. Its 32K
incremental run measured about 1,430 tok/s for newly processed tokens. A
cached 64K request reports the rate for only the appended uncached tokens, so
that number must not be presented as a full-prefix prefill result.

DS4 Qwen 3.8 cold prefill at 9,045 tokens measured about 1,034 tok/s. The
repeat reused 8,192 tokens and completed in about 2.16 seconds.

## Final dev.2 revalidation

The final installed app used the external Application Support configuration,
reported 12 configured models, 11 visible canonical rows, and 12 compatibility
aliases. MLX-Serve and MTPLX duplicates were not present in `/v1/models`.

| Scenario | Result |
| --- | --- |
| MLX-Serve Qwen 3.8 27B text + native unload | `OK`; 1.82 s end to end; 144.8 tok/s backend-reported prefill; model changed from loaded to unloaded while the service stayed up |
| MTPLX Qwen 3.5 4B text | `OK` with `enable_thinking=false`; 0.18 s backend generation; 92.1 tok/s; `reasoning_effort=off` alone was not honored by this backend |
| DS4 High text/stream | `OK`; 160-token stream at 44.2 tok/s backend-reported decode; planned memory 95.94 GiB |
| GLM 5.3 Flash Q2 text/stream | `OK`; 160-token stream at 47.0 tok/s backend-reported decode; planned memory 96.46 GiB |
| DS4 Qwen 3.8 vision | Correctly described a 128 x 128 single-symbol app icon; one image only in this final pass |
| DS4 Qwen 3.8 tool call | Returned one schema-valid `write_note` call; the harness executed it and verified `OK` in a temporary file |
| DS4 Qwen 3.8 repeated long prefix | 32,422-token cold prompt completed in 58.74 s; the appended 32,432-token prompt reused 30,720 tokens and completed in 2.37 s |
| DS4 Qwen 3.8 same-model concurrency | Dispatcher observed two simultaneous in-flight requests; both completed (`3.03 s` and `5.57 s`) without dispatcher serialization |

For the 32K Qwen run, the backend logged 620.9 tok/s average cold prefill
including initial paging and 894.1 tok/s for the 1,712-token appended suffix.
Short streaming samples were about 54-55 tok/s decode. These values are not a
256K stability claim.

## Lifecycle evidence

- Same-model request tracking and cancellation isolation are covered by the
  fake-backend integration suite. The backend remains responsible for actual
  batching, queuing and rate limiting.
- MLX native model unload was exercised. The model became unloaded while the
  reusable MLX server remained healthy.
- DS4 model unload stopped the owned DS4 process because the process is the
  model runtime. Available system memory returned to 95% after the Qwen test.
- The final sequential DS4/GLM/Qwen pass did not increase the system Swapouts
  counter. After the last unload, system-wide free memory returned to about 92%.
- A managed MTPLX process was started on demand and stopped by model unload.
- The always-on external oMLX process remained running during these tests.

## Safety boundary and gaps

- DS4 High and GLM planned roughly 96 GB at their configured 307,200-token
  context. Testing stopped near 4.5K prompt tokens because system free memory
  fell below a comfortable long-run margin.
- DS4 Qwen planned roughly 89 GB at 262,144 tokens. It was validated through
  9K, not through the advertised maximum.
- MLX Flash was validated through 64K, not through 256K.
- MTPLX reasoning control is backend-specific. Its Qwen endpoint honored
  `enable_thinking=false`; a generic `reasoning_effort=off` field did not
  disable reasoning in the direct compatibility check.
- DS4 MXFP4 was not exercised in this run. MTPLX's endpoint returned the
  short text smoke shown above, but the local model assets were not complete;
  therefore MTPLX remains **configured-only for reproducible local lifecycle
  testing**, with no claim of full local load/unload or performance parity.
- Resource-group configuration expresses scheduling policy. It is not, by
  itself, proof of memory safety. Real admission control requires trustworthy
  model estimates plus current host-memory evidence.

## Post-package lifecycle check

After rebuilding commit `5582c5a`, the installed local app started a fresh
dispatcher and cold-loaded MLX-Serve Qwen 3.8 Flash Next through port 18800.
The 17-token prompt returned `OK` with HTTP 200; MLX reported about 85.4
prompt tok/s for this tiny cold sample. A targeted unload then returned HTTP
200, the dispatcher returned to `unloaded`, and MLX reported zero resident
bytes. This is lifecycle evidence, not a representative speed benchmark.

## Reliability closeout checks

The reliability suite also covers backend probe failures (`unknown` state with
last confirmation metadata), dead owned processes, model-state reconciliation,
content-free persistent metric aggregates, dictionary/list model schemas across
the six supported Agents, and environment-referenced backend credentials.
The MLX-Serve Metal GPU timeout path is deterministic-test coverage only; no
production timeout was observed during this dated run. It is limited to one
restart of an InferenceDock-owned managed process and never retries a stream
after response bytes have been sent.

## MLX-Serve long-context observations (2026-09-11)

Added after a second dated run on the same host, still through
`127.0.0.1:18800`. The managed MLX-Serve instance started with
`--ctx-size 262144 --max-resident-models 1 --idle-evict-secs 300 --mtp`, and
per-model settings (`ctx=524288`, `kv=8`, `mtp=on`) were applied from the
engine's own model-settings file and confirmed in its log:
`[model-settings] ... ctx=524288 kv=8 mtp=on`, `mtp=enabled (streaming, depth=6)`.

| Observation | Value |
| --- | --- |
| Longest request served | 183,272 prompt tokens at 524,288 configured context |
| Prefix-cache reuse | 182,184 cached of 183,272 prompt tokens |
| Prefill rate on that request | about 555 tok/s measured on the appended uncached tokens |
| Decode rate on that request | about 24 tok/s at depth 6 MTP |
| Cached-request decode median | about 35.5 tok/s over 849 stored samples |
| Cold load median | about 13.3 s over 38 stored cold starts |

Limits: these are single-host observations with MTP adaptive depth and varying
prompt shapes, not a benchmark. KV 8-bit was active in every sample, so no
KV-8 versus KV-16 comparison exists yet, and the prefill figure covers only the
tokens appended after the cached prefix. A cached request must never be quoted
as full-prefix prefill throughput.

## Settings and cleanup verification (2026-09-11)

- A settings file that still referenced a removed model discarded the whole
  saved policy (smart scheduling and the 300 s idle timeout silently reverted).
  After the fix, unknown IDs are ignored with a warning and the surviving policy
  is kept; this is covered by `test_settings_survive_removed_model_ids`.
- Reviewed cleanup removed one confirmed-missing MTPLX mapping
  (`mtplx-qwen38-flash`) from the local configuration. The core refused the
  request while a request was in flight, created a `0600` sibling backup, and
  the remaining models, aliases, and Agent mappings were unchanged. The unused
  adapter record is what the new reviewer flow now removes as well.
