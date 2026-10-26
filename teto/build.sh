#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build=${TETO_BUILD_DIR:-"$repo/build"}
prefix=${TETO_INSTALL_PREFIX:-"$HOME/.local/share/strawberry-teto/app"}
cmake -S "$repo" -B "$build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DENABLE_GPOD=OFF -DENABLE_STREAMTAGREADER=OFF -DENABLE_TRANSLATIONS=OFF \
  -DBUILD_WERROR=OFF
cmake --build "$build" --target strawberry --parallel "${TETO_BUILD_JOBS:-4}"
cmake --install "$build"
printf 'Built Strawberry. Apply the theme with:\npython3 "%s/teto/install-theme.py" --binary "%s/bin/strawberry"\n' "$repo" "$prefix"
