#!/bin/bash
# Fetch standalone Pythons (python-build-standalone 3.12.13, release 20260623) with numpy and soundfile for the
# packaged apps. Stored in $OPENLIBARY_BAU, otherwise ~/Library/Caches/OpenLibary-bau (not in the code folder).
#   bash app/pythons_holen.sh                 all four (Mac Apple silicon and Intel, Windows x64, Linux x64)
#   bash app/pythons_holen.sh windows linux   only these (mac = both Mac Pythons)
# Needs curl and uv (https://docs.astral.sh/uv/). Versions are pinned so every package is built the same way.
set -euo pipefail
BAU="${OPENLIBARY_BAU:-$HOME/Library/Caches/OpenLibary-bau}"
BASIS="https://releases.astral.sh/github/python-build-standalone/releases/download/20260623"
NUMPY=2.5.4
SOUNDFILE=0.14.0
FEST="cffi==2.1.1 pycparser==3.12 typing_extensions==4.16.0"     # dependencies, pinned as well (THIRD_PARTY.md)
WAHL="${*:-mac windows linux}"
ZIELE=()
for w in $WAHL; do
  case "$w" in
    mac)     ZIELE+=("aarch64-apple-darwin:aarch64-apple-darwin:python/lib/python3.12/site-packages:11.0"
                     "x86_64-apple-darwin:x86_64-apple-darwin:python/lib/python3.12/site-packages:10.15") ;;
    windows) ZIELE+=("x86_64-pc-windows-msvc:x86_64-pc-windows-msvc:python/Lib/site-packages:") ;;
    linux)   ZIELE+=("x86_64-unknown-linux-gnu:x86_64-manylinux_2_28:python/lib/python3.12/site-packages:") ;;
    *)       echo "unknown: $w (mac, windows, linux)" >&2; exit 1 ;;
  esac
done
mkdir -p "$BAU" && cd "$BAU"
for z in "${ZIELE[@]}"; do
  IFS=: read -r t plat sp mac <<< "$z"
  mkdir -p "py-$t"
  [ -f "py-$t/py.tar.gz" ] || [ -d "py-$t/python" ] || \
    curl -sSfL -o "py-$t/py.tar.gz" "$BASIS/cpython-3.12.13%2B20260623-$t-install_only_stripped.tar.gz"
  [ -d "py-$t/python" ] || (cd "py-$t" && tar xzf py.tar.gz)
  # numpy and soundfile per platform (prebuilt wheels only); Intel Mac from macOS 10.15, Linux from glibc 2.28
  MACOSX_DEPLOYMENT_TARGET=${mac:-11.0} uv pip install -q --target "py-$t/$sp" --python-platform "$plat" \
    --python-version 3.12 --only-binary :all: "numpy==$NUMPY" "soundfile==$SOUNDFILE" $FEST
done
du -sh py-*/python
