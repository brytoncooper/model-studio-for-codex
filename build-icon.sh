#!/bin/zsh
set -euo pipefail
source_directory="${0:A:h}"
icon_directory="$(mktemp -d -t model-studio-icon)"
trap 'rmdir "$icon_directory/ModelStudio.iconset" "$icon_directory" 2>/dev/null || true' EXIT
mkdir "$icon_directory/ModelStudio.iconset"
for size in 16 32 128 256 512; do
  /usr/bin/sips -z "$size" "$size" "$source_directory/assets/ModelStudio.png" --out "$icon_directory/ModelStudio.iconset/icon_${size}x${size}.png" >/dev/null
  retina_size=$((size * 2))
  /usr/bin/sips -z "$retina_size" "$retina_size" "$source_directory/assets/ModelStudio.png" --out "$icon_directory/ModelStudio.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$icon_directory/ModelStudio.iconset" -o "$source_directory/assets/ModelStudio.icns"
# Only remove the exact ten generated images, leaving unexpected files intact.
for size in 16 32 128 256 512; do
  /bin/rm "$icon_directory/ModelStudio.iconset/icon_${size}x${size}.png" "$icon_directory/ModelStudio.iconset/icon_${size}x${size}@2x.png"
done
