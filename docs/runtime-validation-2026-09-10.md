# Runtime validation: Apple M5 Max, 128 GB

Date: 2026-09-10

This report records real requests through InferenceDock's `127.0.0.1:18800`
OpenAI-compatible endpoint. Configuration declarations are not counted as
runtime support. A successful short request is not evidence of full context
stability.

## Reproducibility metadata

- Host: Apple M5 Max, 128 GB; macOS 26.6.2 (25G83).
- Dispatcher build lineage used for the final smoke: private commit `f3177c9`;
  later release-only documentation and test changes do not alter the recorded
  inference results.
- Python 3.14.6; Apple Swift 6.2.4.
- DS4 main `1d38311e70f5`; DS4 Qwen runtime `62fa6c71a5cf`.
- MLX-Serve 26.9.2 with MLX 0.32.2.
- Redacted runtime configuration SHA-256:
  `8cb5076fe8ed24b0ef2c13d2ad166e56fe926da14ad64b001688246b7ce5da74`.

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

The MLX Flash cold 9,018-token prefill measured about 1,521 tok/s. Its 32K
incremental run measured about 1,430 tok/s for newly processed tokens. A
cached 64K request reports the rate for only the appended uncached tokens, so
that number must not be presented as a full-prefix prefill result.

DS4 Qwen 3.8 cold prefill at 9,045 tokens measured about 1,034 tok/s. The
repeat reused 8,192 tokens and completed in about 2.16 seconds.

## Lifecycle evidence

- Same-model request tracking and cancellation isolation are covered by the
  fake-backend integration suite. The backend remains responsible for actual
  batching, queuing and rate limiting.
- MLX native model unload was exercised. The model became unloaded while the
  reusable MLX server remained healthy.
- DS4 model unload stopped the owned DS4 process because the process is the
  model runtime. Available system memory returned to 95% after the Qwen test.
- The always-on external oMLX process remained running during these tests.

## Safety boundary and gaps

- DS4 High and GLM planned roughly 96 GB at their configured 307,200-token
  context. Testing stopped near 4.5K prompt tokens because system free memory
  fell below a comfortable long-run margin.
- DS4 Qwen planned roughly 89 GB at 262,144 tokens. It was validated through
  9K, not through the advertised maximum.
- MLX Flash was validated through 64K, not through 256K.
- DS4 MXFP4 and MTPLX were not exercised in this run. MTPLX remains
  **configured only**; its model assets were not locally complete.
- Resource-group configuration expresses scheduling policy. It is not, by
  itself, proof of memory safety. Real admission control requires trustworthy
  model estimates plus current host-memory evidence.
