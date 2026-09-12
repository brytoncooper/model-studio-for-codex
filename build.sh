#!/bin/zsh
set -euo pipefail
source_directory="${0:A:h}"
app_directory="${source_directory:h}/Model Deck.app"
python_executable="$(command -v python3)"
if ! "$python_executable" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "build.sh: python3 on PATH is $("$python_executable" --version); the bridge needs Python 3.11 or newer (put /opt/homebrew/bin first)." >&2
  exit 1
fi
mkdir -p "$app_directory/Contents/MacOS" "$app_directory/Contents/Resources/vendor" "$app_directory/Contents/Helpers"
/bin/zsh "$source_directory/build-icon.sh"
cp "$source_directory/assets/ModelStudio.icns" "$app_directory/Contents/Resources/ModelStudio.icns"
cp "$source_directory/assets/ModelStudio.png" "$app_directory/Contents/Resources/ModelStudio.png"
helper_source="$source_directory/OpenRouterCredentialHelper.swift"
helper_executable="$app_directory/Contents/Helpers/OpenRouterCredentialHelper"
helper_hash_file="$app_directory/Contents/Resources/OpenRouterCredentialHelper.source-sha256"
legacy_helper_hash_file="$app_directory/Contents/Helpers/OpenRouterCredentialHelper.source-sha256"
if [[ -f "$legacy_helper_hash_file" && ! -f "$helper_hash_file" ]]; then
  mv "$legacy_helper_hash_file" "$helper_hash_file"
fi
helper_source_hash="$(/usr/bin/shasum -a 256 "$helper_source" | /usr/bin/awk '{print $1}')"
previous_helper_hash=""
if [[ -f "$helper_hash_file" ]]; then
  previous_helper_hash="$(<"$helper_hash_file")"
fi
if [[ ! -x "$helper_executable" || "$helper_source_hash" != "$previous_helper_hash" ]]; then
  /usr/bin/swiftc -O -swift-version 5 "$helper_source" -o "$helper_executable" -framework Security
  /usr/bin/codesign --force --sign - --identifier com.cooper.codex-openrouter.credential-helper "$helper_executable"
  print -r -- "$helper_source_hash" > "$helper_hash_file"
else
  # Keep the exact signed helper bytes: UI rebuilds must not alter Keychain identity.
  /usr/bin/codesign --verify --strict "$helper_executable"
fi
/bin/rm -rf "$app_directory/Contents/Resources/vendor"
"$python_executable" -m pip install --quiet --disable-pip-version-check --no-compile --target "$app_directory/Contents/Resources/vendor" 'tomlkit==0.13.3'
cp "$source_directory/codex_settings.py" "$app_directory/Contents/Resources/codex_settings.py"
cp "$source_directory/provider_bridge.py" "$app_directory/Contents/Resources/provider_bridge.py"
cp "$source_directory/local_router.py" "$app_directory/Contents/Resources/local_router.py"
cp "$source_directory/chat_wire.py" "$app_directory/Contents/Resources/chat_wire.py"
cp "$source_directory/context_compaction.py" "$app_directory/Contents/Resources/context_compaction.py"
cp "$source_directory/provider_continuation.py" "$app_directory/Contents/Resources/provider_continuation.py"
cp "$source_directory/provider_connections.py" "$app_directory/Contents/Resources/provider_connections.py"
cp "$source_directory/provider_presets.json" "$app_directory/Contents/Resources/provider_presets.json"
cp "$source_directory/agent_message_wire.py" "$app_directory/Contents/Resources/agent_message_wire.py"
cp "$source_directory/cursor_agent.py" "$app_directory/Contents/Resources/cursor_agent.py"
cp "$source_directory/cursor_sdk_runtime.py" "$app_directory/Contents/Resources/cursor_sdk_runtime.py"
cp "$source_directory/model_benchmarks.py" "$app_directory/Contents/Resources/model_benchmarks.py"
cp "$source_directory/spawn_benchmarks.py" "$app_directory/Contents/Resources/spawn_benchmarks.py"
cp "$source_directory/pricing.py" "$app_directory/Contents/Resources/pricing.py"
cp "$source_directory/model_deck_mcp.py" "$app_directory/Contents/Resources/model_deck_mcp.py"
cp "$source_directory/routing_registry.py" "$app_directory/Contents/Resources/routing_registry.py"
cp "$source_directory/provider_usage.py" "$app_directory/Contents/Resources/provider_usage.py"
cp "$source_directory/model_catalog.py" "$app_directory/Contents/Resources/model_catalog.py"
cp "$source_directory/codex_runtime.py" "$app_directory/Contents/Resources/codex_runtime.py"
cp "$source_directory/CodexProviderBridge" "$app_directory/Contents/Resources/CodexProviderBridge"
chmod 755 "$app_directory/Contents/Resources/CodexProviderBridge"
swift_build_directory="$(mktemp -d)"
trap '/bin/rm -rf "$swift_build_directory"' EXIT
cp "$source_directory/OpenRouterSettings.swift" "$swift_build_directory/main.swift"
/usr/bin/swiftc -O -swift-version 5 "$source_directory/UsageDashboard.swift" "$swift_build_directory/main.swift" -o "$app_directory/Contents/MacOS/ModelDeck" -framework AppKit -framework Security -framework ApplicationServices
# Model registrations made by earlier versions call the old executable name for their key lookups.
/bin/ln -sfn ModelDeck "$app_directory/Contents/MacOS/OpenRouterSettings"
/usr/bin/plutil -create xml1 "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleIdentifier -string com.cooper.model-deck "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleName -string 'Model Deck' "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleDisplayName -string 'Model Deck' "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleExecutable -string ModelDeck "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundlePackageType -string APPL "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleShortVersionString -string 2.1 "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleIconFile -string ModelStudio "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert LSMinimumSystemVersion -string 13.0 "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert NSHighResolutionCapable -bool YES "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert PythonExecutable -string "$python_executable" "$app_directory/Contents/Info.plist"
# Never ship bytecode caches: a cache written after signing breaks the seal.
/usr/bin/find "$app_directory" -type d -name __pycache__ -prune -exec /bin/rm -rf {} +
# A certificate-backed signature keeps the app's identity stable across rebuilds, so macOS keeps
# its Accessibility grant. The identity is never auto-picked: name it in MODEL_DECK_SIGNING_IDENTITY
# or in a `signing-identity` file beside this script (exact certificate name or SHA-1 hash).
signing_identity="${MODEL_DECK_SIGNING_IDENTITY:-}"
if [[ -z "$signing_identity" && -f "$source_directory/signing-identity" ]]; then
  signing_identity="$(<"$source_directory/signing-identity")"
fi
if [[ -n "$signing_identity" ]]; then
  if ! /usr/bin/security find-identity -v -p codesigning 2>/dev/null | /usr/bin/grep -Fq "$signing_identity"; then
    echo "build.sh: signing identity \"$signing_identity\" is not in the keychain; refusing to fall back silently." >&2
    exit 1
  fi
  /usr/bin/codesign --force --sign "$signing_identity" --timestamp=none "$app_directory"
  echo "Signed with \"$signing_identity\"; macOS permissions survive rebuilds."
else
  /usr/bin/codesign --force --sign - "$app_directory"
  echo "Ad-hoc signed; macOS will ask for Accessibility again after this rebuild. Set MODEL_DECK_SIGNING_IDENTITY to fix that."
fi
echo "Built $app_directory"
