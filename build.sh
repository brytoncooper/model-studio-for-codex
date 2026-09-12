#!/bin/zsh
set -euo pipefail
source_directory="${0:A:h}"
app_directory="${source_directory:h}/OpenRouter Settings.app"
python_executable="$(command -v python3)"
mkdir -p "$app_directory/Contents/MacOS" "$app_directory/Contents/Resources/vendor" "$app_directory/Contents/Helpers"
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
"$python_executable" -m pip install --quiet --disable-pip-version-check --target "$app_directory/Contents/Resources/vendor" 'tomlkit==0.13.3'
cp "$source_directory/provider_connections.py" "$app_directory/Contents/Resources/provider_connections.py"
cp "$source_directory/provider_presets.json" "$app_directory/Contents/Resources/provider_presets.json"
cp "$source_directory/pricing.py" "$app_directory/Contents/Resources/pricing.py"
cp "$source_directory/codex_settings.py" "$app_directory/Contents/Resources/codex_settings.py"
cp "$source_directory/provider_bridge.py" "$app_directory/Contents/Resources/provider_bridge.py"
cp "$source_directory/routing_registry.py" "$app_directory/Contents/Resources/routing_registry.py"
cp "$source_directory/provider_usage.py" "$app_directory/Contents/Resources/provider_usage.py"
cp "$source_directory/model_catalog.py" "$app_directory/Contents/Resources/model_catalog.py"
cp "$source_directory/codex_runtime.py" "$app_directory/Contents/Resources/codex_runtime.py"
cp "$source_directory/CodexProviderBridge" "$app_directory/Contents/Resources/CodexProviderBridge"
chmod 755 "$app_directory/Contents/Resources/CodexProviderBridge"
/usr/bin/swiftc -O -swift-version 5 "$source_directory/OpenRouterSettings.swift" -o "$app_directory/Contents/MacOS/OpenRouterSettings" -framework AppKit -framework Security
/usr/bin/plutil -create xml1 "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleIdentifier -string com.cooper.codex-openrouter "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleName -string 'OpenRouter Settings' "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleExecutable -string OpenRouterSettings "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundlePackageType -string APPL "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert CFBundleShortVersionString -string 1.5 "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert LSMinimumSystemVersion -string 13.0 "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert NSHighResolutionCapable -bool YES "$app_directory/Contents/Info.plist"
/usr/bin/plutil -insert PythonExecutable -string "$python_executable" "$app_directory/Contents/Info.plist"
/usr/bin/codesign --force --sign - "$app_directory"
echo "Built $app_directory"
