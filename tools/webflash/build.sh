#!/bin/bash
#
# Collect the built firmware images next to the flashing page.
#
# Run after `pio run` in firmware/gs-modem. Copies each board's combined
# factory image -- one file, flashed at offset 0 -- into firmware/ here, and
# writes the manifest the page reads. Everything the page needs then sits in
# this directory, which can be zipped and handed to somebody, or opened from
# a phone hotspot on a launch field.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/../../firmware/gs-modem/.pio/build"
OUT="$HERE/firmware"

if [ ! -d "$BUILD" ]; then
    echo "No PlatformIO build directory. Run 'pio run' in firmware/gs-modem first." >&2
    exit 1
fi

mkdir -p "$OUT"
rm -f "$OUT"/*.bin

boards=()
for dir in "$BUILD"/*/; do
    name="$(basename "$dir")"
    img="$dir/firmware.factory.bin"
    [ -f "$img" ] || continue
    cp "$img" "$OUT/$name.bin"
    size=$(wc -c < "$OUT/$name.bin" | tr -d ' ')
    boards+=("    {\"id\": \"$name\", \"file\": \"firmware/$name.bin\", \"size\": $size}")
    echo "  $name  ($((size / 1024)) KB)"
done

if [ ${#boards[@]} -eq 0 ]; then
    echo "No factory images found. Did the build succeed?" >&2
    exit 1
fi

# Written with a plain loop rather than array slicing: macOS ships bash 3.2,
# where negative subscripts and ${arr[@]:a:b} on the last element do not work.
{
    echo "{"
    echo "  \"built\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
    echo "  \"boards\": ["
    last=$(( ${#boards[@]} - 1 ))
    for i in $(seq 0 $last); do
        if [ "$i" -lt "$last" ]; then
            echo "${boards[$i]},"
        else
            echo "${boards[$i]}"
        fi
    done
    echo "  ]"
    echo "}"
} > "$HERE/manifest.json"

echo
echo "Wrote $HERE/manifest.json"
echo "Serve this directory over HTTPS or localhost:  python3 -m http.server 8000"
