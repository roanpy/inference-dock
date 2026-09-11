# Nightly Real-Engine Runbook

This runbook is for an operator who has explicitly approved real-engine testing. The normal acceptance script never starts one.

1. Record the commit, macOS/Python/Swift versions, active services, ports, a
   redacted config hash, and a RAM/swap baseline. Set a maximum run duration,
   memory threshold, disk threshold, and a stop owner before loading any model.
2. Run `scripts/acceptance.sh` with the example config and archive its output.
3. Test one engine at a time: DS4 (existing Router or managed takeover), MLX, then MTPLX. Record health, `/v1/models`, one streaming request, cancellation, unload, and a second request after switching.
4. Verify that external services were not stopped and that only the selected managed process changed state.
5. Capture `/v1/metrics?limit=N` and stderr with credentials and home paths
   removed. For each model report cold-load and queue time, TTFT, prefill and
   decode speed, cache evidence/source, max/mean/median with sample count and
   time window, peak memory and swap delta, finish reason, and excluded rows.
   Mark MTPLX incomplete if its command/assets are absent. Do not call one
   maximum speed a benchmark.
6. Restore the original Agent/provider configuration and confirm the old endpoint works.
7. Archive evidence and the exact rollback result. Do not publish a compatibility claim from a failed or partial run.

Stop immediately on a port collision, an unexpected process termination,
credential exposure, a request routed to the wrong backend, sustained memory or
swap pressure, disk exhaustion, an error/timeout loop, or a new user request.
Clean up only the run's owned processes and verify external services remain
alive before restoring the original provider configuration.
