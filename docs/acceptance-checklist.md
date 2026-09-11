# Seven-Part Acceptance Checklist

| User requirement | Evidence | Current gate |
| --- | --- | --- |
| 1. Product goal | README scope and non-goals | deterministic smoke pass; real replacement still pending |
| 2. Dispatch rules | resource/exclusive groups and canonical model rows | covered by unit tests; verify with real DS4/MLX/MTPLX runbook |
| 3. Configuration and third-party access | configuration guide, manifests, attribution | public example is sanitized; vendor credentials remain operator-owned |
| 4. Replace existing DS4f/MLX | compatibility matrix and rollback steps | not certified until streaming/cancel/unload gates pass on each runtime |
| 5. Menu and settings | Swift build plus menu structural smoke checks | no-real-model UI checks pass; service stop is shown separately and remains disabled until a verified core stop capability is advertised |
| 6. Implementation and tests | Python tests, Swift build, package/export checks | must pass in CI and on a clean checkout |
| 7. GitHub release | CI workflow, LICENSE, changelog, source archive | release candidate only after all prior gates and attribution review |

## UI and metric regression cases

These cases are mandatory before calling the menu usable:

- A loaded model with `active_requests > 0` is labeled **generating**, while a
  loaded idle model is **loaded**. `busy` must never be rendered as loading.
- Alias IDs sharing `(adapter, backend_model)` produce one canonical row;
  aliases remain callable and visible in details.
- A 409 from a busy switch, an external owner, or an unproven unload is shown
  in Chinese with the structured diagnostic available to copy. The menu does
  not retry it in a loop.
- Cancellation, unload, and service stop are separate actions. A stop action
  cannot target an external service without an explicit reliable capability.
- Run history reads `/v1/metrics` when available and falls back to
  `latest_request` for old cores. Max/mean/median rates include `n`, window and
  excluded samples; only explicit successful finishes and positive rates count.
  Missing fields render `unknown`.
- A reasoning-only delta, tool delta, incomplete SSE EOF, `client_disconnect`,
  or `request_cancelled` is never silently turned into a successful `Working`
  completion.

Do not describe the project as a complete replacement while any row is marked pending.
