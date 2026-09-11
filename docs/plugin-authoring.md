# Plugin authoring

InferenceDock plugins are YAML data, never executable Python. A manifest uses
`schema_version: 1`, a kebab-case `id`, `discover`, and `service` blocks.
`service.mode` is `managed`, `external`, or `observe`; `endpoint` must be a
loopback HTTP URL. Optional `ownership`, `start_argv`, `stop_strategy`,
`activation`, `capabilities`, `metrics`, `exclusive_group`, and
`keep_resident` describe a plan only. `start_argv` is an argv array and never
a shell command.

`plugin_runtime.py` compiles manifests and DS4 `local-runtime.json` into a
preview. It does not start/stop services, call launchctl, alter DS4 files, or
apply plist changes. Unknown fields, unsafe URLs/paths, duplicate aliases, and
secret fields are rejected.

`plugin_runtime.py plan --id ds4 --out <empty-dir>` writes a reviewable action
plan: per-model `/_router/switch` activation calls, per-variant argv fragments
(vision/MTP/SSD), router timeouts, a rollback capture template, and plist
previews. Preview files are only written to the given empty directory.

`plugin_runtime.py takeover [--ds4-config PATH] [--out <empty-dir>]` compiles
DS4 `local-runtime.json` plus each service's existing launchd plist into
independent managed adapters: the ds4 service expands per advertised variant
with `--model/--ctx/--tokens` and runtime flags rewritten, other services keep
their plist argv verbatim. Unknown variant/service references, duplicate JSON
keys, shell syntax, non-absolute executables, and unsafe environment keys are
rejected. Output is a validated config preview plus a rollback capture
template; nothing is executed and no plist is modified.

`--base-config PATH --drop-adapter NAME` merges an existing engines config:
dropped adapters and their models are removed, all other adapters/models and
listen/timeout settings are inherited, and takeover adapters overlay on top.

Manifests may declare `repositories` entries (`path`, `remote`, `branch`) and
`service.activation`/`service.deactivation` action templates (`http` with a
safe relative path, or `argv`). `update_monitor.py [--id ID] [--offline]`
compares registered local worktrees with remote heads using read-only
`git ls-remote`; it never fetches, mutates third-party repos, or applies
updates. Per-repository failures are isolated into that entry's `error`.
