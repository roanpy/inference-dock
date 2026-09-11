# Migration helper

`scripts/migration_helper.py` creates a file-level migration plan for moving
old agent or shell entry points to the validated dispatcher endpoint. It is
read-only by default and never connects to, stops, or modifies DS4, MLX-Serve,
MTPLX, oMLX, launchd, or another backend.

```sh
python3 scripts/migration_helper.py \
  --config config/engines.example.yaml \
  path/to/agent.env path/to/old-script.sh
```

The plan rewrites only two kinds of data:

- loopback URLs whose ports are declared as legacy adapter endpoints in the
  target config;
- selected default-model environment variables whose exact value maps to a
  public model ID in the target config.

References to legacy launchers such as `ds4`, `mlx-serve`, or `mtplx` are
reported for manual review. The helper does not guess process ownership or
replace arbitrary startup commands.

Use repeatable `--legacy-port` options to restrict endpoint migration to known
legacy ports. This prevents an unrelated local engine from being rewritten.

Applying changes requires both `--apply` and explicit file arguments. Each
changed file receives a same-directory timestamped backup, then is replaced
atomically:

```sh
python3 scripts/migration_helper.py \
  --config config/engines.yaml \
  --apply path/to/agent.env
```

Symlinks, missing files, binary files, unreadable files, and files larger than
1 MiB are skipped. A file changed after its plan was generated is refused.
Review the JSON plan and keep the backup until the migrated agent has passed
its own checks.
