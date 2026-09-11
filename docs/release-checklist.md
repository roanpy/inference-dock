# Developer preview release gate

This is a release checklist, not a claim that every engine has passed testing.

## Scope

- Declarative local discovery and import previews.
- Managed process routing and an optional macOS menu app.
- External/resident services remain outside automatic shutdown control.
- No model downloads, Git merges, engine patching or unattended publication.

## Required checks

1. Run dispatcher and plugin fixture tests without production services.
2. Build the Swift app and check packaging does not use personal configuration.
3. Export allowlisted sources without local Git history, logs or model data.
4. Extract the archive into a fresh directory and run tests there.
5. Inspect dependency declaration, license, archive paths and credential findings.
6. Record tested and untested features accurately in release notes.

Only after these pass, choose a public repository and upload the reviewed source.
Do not publish the local development repository wholesale: historical personal
configuration must be reviewed separately. The private/public repository split,
export hash, version tag and rollback process are defined in
[RELEASING.md](../RELEASING.md). No remote action is automated here.

## Remaining integration gates

Each real backend needs independent lifecycle, stream, cancellation and model
capability tests before being described as production supported. Discovery alone
does not meet this gate. Multiple heavy models and long-context stress testing
are excluded from the smoke suite.

## Ongoing maintenance

Release small adapter updates when public engine contracts change. Keep schema
versions explicit and retain reproducible fixtures. Record upstream versions,
not assumed support for every future version. A plugin that no longer matches
must report incompatibility instead of guessing a new command.
