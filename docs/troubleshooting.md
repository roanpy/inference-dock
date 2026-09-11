# Troubleshooting and Rollback

## Common states

- **offline**: the menu cannot reach port 18800. Check the bundled Python/PyYAML and `/health` before touching an engine.
- **unavailable**: an endpoint or managed command failed its health probe. Read the short Chinese summary first; expand the native diagnostic disclosure for HTTP details.
- **external ownership conflict**: another service owns the resource group. Stop or reconfigure that service manually, or keep it as an external adapter; InferenceDock will not terminate it.
- **MTPLX draft**: the manifest is valid but `start_argv` or model assets are missing. Add them explicitly; refresh is intentionally non-starting.
- **busy**: cancellation is a separate action from unload. Cancel the request first, wait for zero active requests, then unload.

## Known failure reports

- **HTTP 409 while switching**: this is an intentional safety result when an
  in-flight request, an external owner, or an unproven unload blocks the target.
  Do not retry in a loop; inspect the structured `error.type`, wait for the
  active request to finish, or perform the external rollback manually.
- **The client stays `Working`**: check the final SSE `finish_reason`, `[DONE]`,
  and the dispatcher row. An EOF without an explicit finish is incomplete, not
  success. A client disconnect is recorded as `client_disconnect`; it must not
  be relabeled `stop`. Reconnect only after `active_requests` returns to zero.
- **Duplicate model rows**: the menu groups by adapter plus canonical backend
  model. Alias IDs may remain callable, but they should not create a second
  visible row. If two rows remain, inspect `backend_model` and the adapter
  identity rather than deleting model files.
- **Reasoning metadata is missing or mixed with content**: verify the request
  body, backend delta type and `reasoning_levels` mapping. A longer answer is
  not proof that `high` or `max` reached the backend. Preserve reasoning/tool
  deltas and content as separate stream events.
- **Status drifts between loaded and generating**: `loaded` means residency;
  `generating` requires an active request. A healthy service is not proof that a
  model is loaded, and an `active_requests=0` observation through another port
  is not proof that the service is idle.

## Safe checks

```sh
python3 scripts/model_dispatch.py --config config/engines.example.yaml --check-config --probe
python3 scripts/plugin_runtime.py preview --plugins-dir plugins --pretty
python3 tests/run_tests.py
```

These commands use the repository example and do not start a real model. Collect the status JSON, adapter name, endpoint, and exit code; do not paste credentials or full private paths into an issue.

## Rollback

Restore the backed-up Agent/provider file, point the client back to its original endpoint, and leave external servers running unless you started them from a managed manifest. Remove only the generated app bundle or a managed process. Never delete model weights or a user config as part of rollback.
