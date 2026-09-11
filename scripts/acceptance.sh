#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
ACCEPTANCE_APP=$(mktemp -d "${TMPDIR:-/tmp}/inference-dock-acceptance.XXXXXX")/InferenceDock.app

python3 -m py_compile scripts/fake_backend.py scripts/model_dispatch.py \
  scripts/agent_config.py \
  scripts/plugin_registry.py scripts/plugin_runtime.py scripts/migration_helper.py \
  scripts/update_monitor.py \
  tests/run_tests.py tests/test_plugin_registry.py tests/test_plugin_runtime.py \
  tests/test_migration_helper.py tests/test_update_monitor.py tests/test_export_source.py
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "scripts")
import model_dispatch
model_dispatch.load_config(Path("config/engines.example.yaml"))
PY
python3 tests/run_tests.py
python3 tests/test_agent_config.py
python3 tests/test_plugin_registry.py
python3 tests/test_plugin_runtime.py
python3 tests/test_migration_helper.py
python3 tests/test_update_monitor.py
python3 tests/test_export_source.py
swift build
for pattern in 'openSettingsWindow' 'settings.tab.server' 'settings.tab.models' 'settings.tab.policy' 'settings.tab.agents' 'settings.tab.diagnostics' 'settings.server.instancePreview' 'settings.server.previewEndpoint' 'settings.server.previewPath' 'settings.server.notSaved' 'importEndpointPreview' 'importPathPreview' 'runPluginImportPreview' 'settings.server.unsupportedActions' 'settings.capabilities.heading' 'settings.policy.exclusive' 'settings.policy.resident' 'settings.disable' 'settings.delete' 'settings.updates.rollback' 'systemSettings' 'checkUpdates' 'runHistory' 'showModelDetails' 'stopCore' 'DiscoveryDiagnostic' 'dialog.discovery.diagnostics' 'state.external' 'state.disabled' 'details.configured' 'details.effective' 'maxOutputTokens' 'max' 'median' 'unknown' 'MetricsPayload' '/v1/metrics' '/v1/reload' 'update_monitor.py' 'SMAppService' 'loginLaunchPreferred' '/v1/settings' '/v1/cancel' 'adapter_policies' 'model_policies' 'exclusive_groups' 'estimated_memory_gb' '/v1/prune-models' 'persistentSummaries' 'unusedAdapters' 'settings.diagnostics.configuration' 'builtinPluginTemplates' 'loadPluginTemplates'; do
  grep -Eq -- "$pattern" apps/ModelDispatchMenu/Sources/ModelDispatchMenu/main.swift
done
for locale in Base.lproj en.lproj zh-Hans.lproj; do
  test -f "apps/ModelDispatchMenu/Resources/$locale/Localizable.strings"
done
# Settings pages must keep their rows packed at the top: a stack stretched to
# the container height spreads every row and pushes the last controls below the
# fold, which is the layout bug this guard prevents from coming back.
grep -q 'container.heightAnchor.constraint(equalTo: stack.heightAnchor)' apps/ModelDispatchMenu/Sources/ModelDispatchMenu/main.swift
grep -q 'stack.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor)' apps/ModelDispatchMenu/Sources/ModelDispatchMenu/main.swift
if grep -q 'stack.bottomAnchor.constraint(equalTo: container.bottomAnchor)' apps/ModelDispatchMenu/Sources/ModelDispatchMenu/main.swift; then
  echo "settings page stretches its stack to the container height" >&2
  exit 2
fi
LOCALE_KEYS=$(mktemp -d "${TMPDIR:-/tmp}/inference-dock-locale.XXXXXX")
for locale in Base.lproj en.lproj zh-Hans.lproj; do
  sed -n 's/^"\([^"]*\)"[[:space:]]*=.*/\1/p' "apps/ModelDispatchMenu/Resources/$locale/Localizable.strings" | sort > "$LOCALE_KEYS/$locale"
done
cmp -s "$LOCALE_KEYS/Base.lproj" "$LOCALE_KEYS/en.lproj"
cmp -s "$LOCALE_KEYS/Base.lproj" "$LOCALE_KEYS/zh-Hans.lproj"
grep -Eo 'T\("[^"]+' apps/ModelDispatchMenu/Sources/ModelDispatchMenu/main.swift \
  | sed 's/^.*T("//' | sort -u > "$LOCALE_KEYS/code"
if comm -23 "$LOCALE_KEYS/code" "$LOCALE_KEYS/en.lproj" | grep -q .; then
  echo "Swift localization key is missing from Localizable.strings" >&2
  exit 2
fi
PYTHON_BIN=$(command -v python3)
MODEL_DISPATCH_APP_OUTPUT="$ACCEPTANCE_APP" MODEL_DISPATCH_CONFIG=config/production.example.yaml MODEL_DISPATCH_PYTHON="$PYTHON_BIN" sh scripts/package_app.sh >/dev/null
cmp -s config/production.example.yaml "$ACCEPTANCE_APP/Contents/Resources/model-dispatch-config/engines.yaml"
MODEL_DISPATCH_APP_OUTPUT="$ACCEPTANCE_APP" sh scripts/package_app.sh >/dev/null
NESTED_OUTPUT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/inference-dock-nested-output.XXXXXX")
NESTED_APP="$NESTED_OUTPUT_ROOT/new/deep/InferenceDock.app"
MODEL_DISPATCH_APP_OUTPUT="$NESTED_APP" sh scripts/package_app.sh >/dev/null
test -x "$NESTED_APP/Contents/MacOS/ModelDispatchMenu"
test -x "$ACCEPTANCE_APP/Contents/MacOS/ModelDispatchMenu"
test -f "$ACCEPTANCE_APP/Contents/MacOS/model-dispatch-core.py"
test -f "$ACCEPTANCE_APP/Contents/scripts/fake_backend.py"
test -f "$ACCEPTANCE_APP/Contents/scripts/plugin_registry.py"
test -f "$ACCEPTANCE_APP/Contents/scripts/plugin_runtime.py"
test -f "$ACCEPTANCE_APP/Contents/scripts/migration_helper.py"
test -f "$ACCEPTANCE_APP/Contents/scripts/update_monitor.py"
test -f "$ACCEPTANCE_APP/Contents/scripts/agent_config.py"
test -f "$ACCEPTANCE_APP/Contents/Resources/AppIcon.icns"
test -f "$ACCEPTANCE_APP/Contents/Resources/Base.lproj/Localizable.strings"
test -f "$ACCEPTANCE_APP/Contents/Resources/en.lproj/Localizable.strings"
test -f "$ACCEPTANCE_APP/Contents/Resources/zh-Hans.lproj/Localizable.strings"
test -d "$ACCEPTANCE_APP/Contents/plugins"
printf 'keep-existing-app\n' > "$ACCEPTANCE_APP/Contents/.existing-app-sentinel"
if MODEL_DISPATCH_APP_OUTPUT="$ACCEPTANCE_APP" MODEL_DISPATCH_TEST_FAIL_BEFORE_INSTALL=1 sh scripts/package_app.sh >/dev/null 2>&1; then
  echo "staging fault injection unexpectedly installed an app" >&2
  exit 2
fi
test -f "$ACCEPTANCE_APP/Contents/.existing-app-sentinel"
"$PYTHON_BIN" "$ACCEPTANCE_APP/Contents/scripts/update_monitor.py" --plugins-dir "$ACCEPTANCE_APP/Contents/plugins" --offline >/dev/null
test -f "$ACCEPTANCE_APP/Contents/Resources/model-dispatch-config/engines.yaml"
cmp -s config/engines.example.yaml "$ACCEPTANCE_APP/Contents/Resources/model-dispatch-config/engines.yaml"
OUTSIDE_CONFIG=$(mktemp "${TMPDIR:-/tmp}/inference-dock-outside-config.XXXXXX")
cp config/engines.example.yaml "$OUTSIDE_CONFIG"
if MODEL_DISPATCH_APP_OUTPUT="$ACCEPTANCE_APP" MODEL_DISPATCH_CONFIG="$OUTSIDE_CONFIG" sh scripts/package_app.sh >/dev/null 2>&1; then
  echo "Public package unexpectedly accepted a config outside the repository" >&2
  exit 2
fi
test -f "$ACCEPTANCE_APP/Contents/.existing-app-sentinel"
rm -f "$OUTSIDE_CONFIG"
! grep -ERnI '/Users/|local\.preview' "$ACCEPTANCE_APP/Contents"
! grep -ERnIi '/Users/[A-Za-z0-9._-]+/|/private/var/[A-Za-z0-9._-]+/|Bearer[[:space:]]+[A-Za-z0-9._-]{12,}' "$ACCEPTANCE_APP/Contents"
! find "$ACCEPTANCE_APP/Contents" -type f \( -name '*.gguf' -o -name '*.safetensors' -o -name '*.bin' -o -name '*.log' -o -name '*.jsonl' \) -print -quit | grep -q .

# Verify that the reviewed public exporter is deterministic and self-contained.
ARCHIVE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/inference-dock-export.XXXXXX")
ARCHIVE="$ARCHIVE_DIR/inference-dock.tar.gz"
python3 scripts/export_source.py "$ARCHIVE" >/dev/null
ARCHIVE_2="$ARCHIVE_DIR/inference-dock-repeat.tar.gz"
python3 scripts/export_source.py "$ARCHIVE_2" >/dev/null
cmp -s "$ARCHIVE" "$ARCHIVE_2"
! tar -tzf "$ARCHIVE" | grep -En '(^|/)(logs|cache|dist|\.build|\.venv)(/|$)|(/Users/|\.safetensors$|\.bin$)'
EXTRACT="$ARCHIVE_DIR/source"
mkdir -p "$EXTRACT"
tar -xzf "$ARCHIVE" -C "$EXTRACT"
(cd "$EXTRACT/inference-dock" && python3 -m py_compile scripts/*.py)
for required in README.md README.zh-CN.md SECURITY.md CONTRIBUTING.md THIRD_PARTY_NOTICES.md RELEASING.md .github/workflows/ci.yml; do
  test -f "$EXTRACT/inference-dock/$required"
done
test -f "$EXTRACT/inference-dock/scripts/agent_config.py"
