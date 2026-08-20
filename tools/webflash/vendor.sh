#!/bin/bash
#
# Bundle the flasher locally, so the page works with no network.
#
# This downloads third-party code (esptool-js, Espressif, Apache-2.0) into
# vendor/. That is a deliberate choice and the reason it is a separate,
# explicit step rather than something build.sh does behind your back: the
# rest of this project has no third-party runtime dependencies at all.
#
# Skip it and the page falls back to a CDN, which is fine at a desk and
# useless on a launch field.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/vendor/esptool-js"
VERSION="${1:-0.5.4}"

mkdir -p "$DEST"
echo "Fetching esptool-js $VERSION..."
curl -fsSL "https://unpkg.com/esptool-js@${VERSION}/bundle.js" -o "$DEST/bundle.js"
echo "Wrote $DEST/bundle.js ($(wc -c < "$DEST/bundle.js" | tr -d ' ') bytes)"
echo "The page will now use it and work offline."
