#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)
APP="${MODEL_DISPATCH_APP_OUTPUT:-$ROOT/dist/InferenceDock.app}"
MACOS="$APP/Contents/MacOS"
RESOURCES="$APP/Contents/Resources"
# Public builds use an explicit repository config. A private config is opt-in via both variables.
CONFIG_SOURCE="${MODEL_DISPATCH_CONFIG:-$ROOT/config/engines.example.yaml}"
PUBLIC_MODE="${MODEL_DISPATCH_PUBLIC:-1}"
PYTHON_BIN="${MODEL_DISPATCH_PYTHON:-$(command -v python3 || true)}"

cd "$ROOT"
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "Python 3 not found; install Python 3 or set MODEL_DISPATCH_PYTHON" >&2
  exit 2
fi
if ! "$PYTHON_BIN" -c 'import yaml' >/dev/null 2>&1; then
  echo "PyYAML is required by model-dispatch-core.py; install it for $PYTHON_BIN" >&2
  exit 2
fi
if [ ! -f "$CONFIG_SOURCE" ]; then
  echo "Config not found: $CONFIG_SOURCE" >&2
  exit 2
fi
case "$CONFIG_SOURCE" in
  /*) : ;;
  *) CONFIG_SOURCE="$ROOT/$CONFIG_SOURCE" ;;
esac
CONFIG_SOURCE_REAL=$("$PYTHON_BIN" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CONFIG_SOURCE")
if [ "$PUBLIC_MODE" = "1" ] || [ "$PUBLIC_MODE" = "true" ]; then
  case "$CONFIG_SOURCE_REAL" in
    "$ROOT"/*) : ;;
    *) echo "Public builds require a config inside the repository; set MODEL_DISPATCH_PUBLIC=0 for a private build" >&2; exit 2 ;;
  esac
  CONFIG_SOURCE=$CONFIG_SOURCE_REAL
  if grep -Eni -- '(/Users/|/private/var/|~/(Library|Developer|\.cache)|\.(gguf|safetensors|bin)([[:space:]]|$)|api[_-]?key[[:space:]]*[:=]|password[[:space:]]*[:=]|secret[[:space:]]*[:=]|Bearer[[:space:]]+[A-Za-z0-9._-]+)' "$CONFIG_SOURCE" >/dev/null; then
    echo "Public config contains a private path, credential, log, or model artifact: $CONFIG_SOURCE" >&2
    exit 2
  fi
fi
swift build -c release

rm -rf "$APP"
mkdir -p "$MACOS" "$RESOURCES/model-dispatch-config" "$APP/Contents/scripts" \
  "$RESOURCES/Base.lproj" "$RESOURCES/en.lproj" "$RESOURCES/zh-Hans.lproj"
cp .build/release/ModelDispatchMenu "$MACOS/ModelDispatchMenu"
cp scripts/model_dispatch.py "$MACOS/model-dispatch-core.py"
cp scripts/fake_backend.py "$APP/Contents/scripts/fake_backend.py"
cp scripts/plugin_registry.py "$APP/Contents/scripts/plugin_registry.py"
cp scripts/plugin_runtime.py "$APP/Contents/scripts/plugin_runtime.py"
cp scripts/migration_helper.py "$APP/Contents/scripts/migration_helper.py"
cp scripts/update_monitor.py "$APP/Contents/scripts/update_monitor.py"
cp -R plugins "$APP/Contents/plugins"
cp "$CONFIG_SOURCE" "$RESOURCES/model-dispatch-config/engines.yaml"
cp assets/AppIcon.icns "$RESOURCES/AppIcon.icns"
cp apps/ModelDispatchMenu/Resources/Base.lproj/Localizable.strings "$RESOURCES/Base.lproj/Localizable.strings"
cp apps/ModelDispatchMenu/Resources/en.lproj/Localizable.strings "$RESOURCES/en.lproj/Localizable.strings"
cp apps/ModelDispatchMenu/Resources/zh-Hans.lproj/Localizable.strings "$RESOURCES/zh-Hans.lproj/Localizable.strings"
chmod +x "$MACOS/ModelDispatchMenu"

if [ "$PUBLIC_MODE" = "1" ] || [ "$PUBLIC_MODE" = "true" ]; then
  # Scan the assembled app as well as the input config. This catches a private
  # path accidentally introduced by a copied manifest or helper file.
  if grep -ERnIi \
    -- '(/Users/[A-Za-z0-9._-]+/|/private/var/[A-Za-z0-9._-]+/|api[_-]?key[[:space:]]*[:=][[:space:]]*[^$<{[:space:]]|password[[:space:]]*[:=][[:space:]]*[^$<{[:space:]]|secret[[:space:]]*[:=][[:space:]]*[^$<{[:space:]]|Bearer[[:space:]]+[A-Za-z0-9._-]{12,})' \
    "$MACOS/model-dispatch-core.py" "$APP/Contents/scripts" "$APP/Contents/plugins" "$RESOURCES/model-dispatch-config" >/dev/null; then
    echo "Public app contains a private path, credential, log, or model artifact: $APP" >&2
    exit 2
  fi
  if find "$APP/Contents" -type f \( -name '*.gguf' -o -name '*.safetensors' -o -name '*.bin' \) -print -quit | grep -q .; then
    echo "Public app contains a model artifact: $APP" >&2
    exit 2
  fi
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>ModelDispatchMenu</string>
  <key>CFBundleIdentifier</key>
  <string>local.model-dispatch.menu</string>
  <key>CFBundleName</key>
  <string>InferenceDock</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleLocalizations</key>
  <array>
    <string>en</string>
    <string>zh-Hans</string>
  </array>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST

printf '%s\n' "$APP"
