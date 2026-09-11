# Platform adapters

InferenceDock discovers installed runtimes and imports selected configuration.
Discovery is not a claim that every model, version or hardware configuration works.
Model downloads, engine updates and Git merges remain with the original tools.

| Platform | Integration target | Lifecycle policy |
| --- | --- | --- |
| DS4 | Existing router and separate main/experimental executables | External until explicit ownership migration |
| MLX-Serve | Existing OpenAI endpoint | External; model switching must follow its own API |
| MTPLX | Local CLI and cached model configuration | Managed only for processes launched by this tool |
| oMLX | Existing endpoint | Observe; preserve RAG and resident services |
| Ollama | Installed CLI and existing endpoint | Discovery/observe candidate; no implicit shutdown |
| LM Studio | Existing endpoint and CLI | Discovery/observe candidate |
| mlx-lm | Installed server entry point | Candidate, requires version-specific configuration |
| mlx-vlm | Installed vision server entry point | Candidate; vision must be verified per model |
| vllm-mlx | Installed server and model registry | Candidate; distinguish single-model and registry modes |
| llama.cpp | Installed llama-server | Candidate for managed argv integration |

Unverified candidates do not get automatic process control. A new adapter should
declare discovery rules, configuration readers, supported actions and evidence.
Never infer a runnable executable from a Git branch name alone.

## What belongs in this tool

| Include | Keep with the engine or its existing tools |
| --- | --- |
| Executable/config discovery, manual paths | Downloading models or repositories |
| Read-only configuration preview with provenance | Editing arbitrary backend source or build scripts |
| Stable public model IDs and runtime selection | Git checkout, merge, rebase or engine compilation |
| Owned process lifecycle and verified API calls | Global process killing or reclaiming resident RAG services |
| Explicit compatibility/version information | Assuming every upstream update remains compatible |
| Backend-supported parameter overrides | Reimplementing KV cache, MTP or vision preprocessing |
| Passive timing and bounded optional tests | Unattended stress tests or automatic best-model selection |

Discovery, import and lifecycle support are separate levels. A platform can be
listed by the registry without permission to load or stop anything. Versioned
configuration readers should import only known fields, leaving unknown settings
with their original owner. Request-level settings and restart-required settings
must be distinguished before any future apply action.

## Updating an adapter

1. Record the engine version and changed public CLI/API contract.
2. Update the smallest manifest/reader involved.
3. Run fixture tests without loading a real model.
4. Validate against the intended engine in an idle test window.
5. Publish the compatibility notes with the adapter change.

Plugin and application releases are manual initially. No scheduled publishing,
automatic engine update or repository mutation is implied.

## Sources

- https://github.com/ml-explore/mlx-lm
- https://github.com/Blaizzy/mlx-vlm
- https://github.com/waybarrios/vllm-mlx/blob/main/docs/guides/server.md
- https://github.com/mostlygeek/llama-swap

llama-swap is a reuse candidate, not a bundled dependency. The existing dispatcher
is experimental until lifecycle compatibility is validated for the selected backend.
