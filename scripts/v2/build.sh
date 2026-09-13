#!/bin/zsh
set -euo pipefail

repository_root="$(cd "$(dirname "$0")/../.." && pwd)"
python_executable=""
output_root=""
scratch_root=""

while (( $# )); do
  case "$1" in
    --python-executable)
      python_executable="$2"
      shift 2
      ;;
    --output)
      output_root="$2"
      shift 2
      ;;
    --scratch)
      scratch_root="$2"
      shift 2
      ;;
    *)
      print -u2 "usage: build.sh --python-executable ABSOLUTE_PATH --output FRESH_ABSOLUTE_DIRECTORY [--scratch FRESH_ABSOLUTE_DIRECTORY]"
      exit 2
      ;;
  esac
done

if [[ "$python_executable" != /* || ! -x "$python_executable" ]]; then
  print -u2 "--python-executable must be an executable absolute path"
  exit 2
fi
if [[ "$output_root" != /* ]]; then
  print -u2 "--output must be a fresh absolute directory"
  exit 2
fi
if [[ -e "$output_root" ]]; then
  print -u2 "output already exists: $output_root"
  exit 2
fi
if [[ -n "$scratch_root" ]]; then
  if [[ "$scratch_root" != /* || -e "$scratch_root" ]]; then
    print -u2 "--scratch must be a fresh absolute directory"
    exit 2
  fi
  mkdir "$scratch_root"
else
  scratch_root="$(mktemp -d /tmp/model-deck-v2-swift.XXXXXX)"
fi

swift build \
  --package-path "$repository_root/macos" \
  --scratch-path "$scratch_root" \
  --configuration release \
  --product ModelDeckV2

binary_root="$(swift build \
  --package-path "$repository_root/macos" \
  --scratch-path "$scratch_root" \
  --configuration release \
  --show-bin-path)"
binary="$binary_root/ModelDeckV2"
if [[ ! -x "$binary" ]]; then
  print -u2 "V2 release binary was not produced at $binary"
  exit 1
fi

mkdir "$output_root"
application="$output_root/Model Deck V2.app"
mkdir -p "$application/Contents/MacOS"
mkdir -p "$application/Contents/Resources/python/src"
mkdir -p "$application/Contents/Resources/config"

/usr/bin/ditto "$binary" "$application/Contents/MacOS/ModelDeckV2"
chmod 755 "$application/Contents/MacOS/ModelDeckV2"
/usr/bin/rsync -a \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$repository_root/python/src/" \
  "$application/Contents/Resources/python/src/"

for bundle_name in ModelDeck_ModelDeckContracts.bundle ModelDeck_ModelDeckPresentation.bundle; do
  bundle_source="$binary_root/$bundle_name"
  if [[ ! -d "$bundle_source" ]]; then
    print -u2 "required Swift resource bundle is missing: $bundle_source"
    exit 1
  fi
  /usr/bin/ditto "$bundle_source" "$application/$bundle_name"
done

info_plist="$application/Contents/Info.plist"
/usr/bin/plutil -create xml1 "$info_plist"
/usr/bin/plutil -insert CFBundleName -string "Model Deck V2" "$info_plist"
/usr/bin/plutil -insert CFBundleDisplayName -string "Model Deck V2" "$info_plist"
/usr/bin/plutil -insert CFBundleIdentifier -string "com.coopertechnology.modeldeck.v2.dev" "$info_plist"
/usr/bin/plutil -insert CFBundleExecutable -string "ModelDeckV2" "$info_plist"
/usr/bin/plutil -insert CFBundlePackageType -string "APPL" "$info_plist"
/usr/bin/plutil -insert CFBundleShortVersionString -string "2.0-dev" "$info_plist"
/usr/bin/plutil -insert CFBundleVersion -string "1" "$info_plist"
/usr/bin/plutil -insert LSMinimumSystemVersion -string "13.0" "$info_plist"
/usr/bin/plutil -insert NSHighResolutionCapable -bool true "$info_plist"

runtime_plist="$application/Contents/Resources/config/runtime.plist"
/usr/bin/plutil -create xml1 "$runtime_plist"
/usr/bin/plutil -insert python_executable -string "$python_executable" "$runtime_plist"

print "Built $application"
print "Swift scratch retained at $scratch_root"
