# Staged app packaging (B04.resources)

Finalizer-owned staging mirrors legacy `build.sh` output without mutating production `build.sh` during B04:

- `Contents/MacOS/ModelDeck` executable
- `Contents/MacOS/OpenRouterSettings` symlink to ModelDeck
- `Contents/Helpers/OpenRouterCredentialHelper` with stable hash
- `provider_presets.json`, icons, Python resources copied beside the binary

SwiftPM build: `cd macos && swift build -c release --product ModelDeck`
