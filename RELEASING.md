# Releasing InferenceDock

The repository is intentionally split into two histories, following the
maintainer's existing public/private project pattern:

- Private development repository: full development history, local
  deployment notes and reviewed backup material. Never publish this repository.
- `roanpy/inference-dock` (public): a clean history created only from the
  reviewed source allowlist. It must not be a mirror of the private worktree.

No script in this repository creates a remote, pushes a branch, or opens a
release. The maintainer confirms the exact owner, repository visibility, tag,
and release version before any external write.

## Public release procedure

1. Record the source commit, upstream engine versions, and the Developer
   Preview status. Keep real-model and night-run evidence separate from smoke
   evidence.
2. Run `python3 tests/run_tests.py`, each plugin test, `swift build`, and
   `sh scripts/acceptance.sh` in a clean checkout. Do not start a real model.
3. Export with `python3 scripts/export_source.py dist/inference-dock-source.tar.gz`.
   Inspect the file list and SHA-256; the exporter rejects personal paths,
   obvious credentials, private logs/caches, and model artifacts.
4. Extract the archive into a new temporary directory, run the same static
   checks there, and confirm `README.md`, `README.zh-CN.md`, `LICENSE`,
   `SECURITY.md`, CI and all support documents are present.
5. Create or update the public repository from the extracted tree, add a
   reviewed tag, then enable the GitHub Actions workflow. Never copy
   `config/engines.yaml`, `config/local.preview.yaml`, `.build`, `dist`,
   `logs`, model weights, prompts, credentials, or personal paths.
6. Attach only the public archive and checksums. Keep a private manifest linking
   the public tag to the source commit and export hash.

## Rollback

If a release contains a bad package or unsafe claim, mark the GitHub release as
withdrawn, publish a corrected tag, and point users to the previous verified
tag. Do not rewrite the private development history. Restore a local setup by
using the saved config backup and stopping only processes owned by
InferenceDock; never delete model files or external services during rollback.

## Version and evidence rules

Use a numbered release only after the maintainer has reviewed the public export.
Until real MLX-Serve/DS4/MTPLX checks pass, keep `Developer Preview` in the
README and release notes. A config declaration, discovery result, short request,
or one maximum speed does not establish lifecycle support, long-context safety,
or a benchmark.
