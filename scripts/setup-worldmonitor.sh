#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
wm_dir="$PWD/.services/worldmonitor"
wm_revision=c0f783620dcdae713a6d28c239844c12311e2bf6
mkdir -p .services .cache/npm
if [[ ! -d "$wm_dir/.git" ]]; then
  git clone --filter=blob:none --no-checkout https://github.com/koala73/worldmonitor.git "$wm_dir"
  git -C "$wm_dir" fetch --depth=1 origin "$wm_revision"
  git -C "$wm_dir" checkout --detach "$wm_revision"
fi
if [[ "$(git -C "$wm_dir" rev-parse HEAD)" != "$wm_revision" ]]; then
  echo 'World Monitor revision differs; preserve the checkout and review it before setup.' >&2
  exit 1
fi
cd -- "$wm_dir"
npm ci --ignore-scripts --no-audit --no-fund --cache ../../.cache/npm
node node_modules/esbuild/install.js
npm run inventory:facts
echo 'Pinned local World Monitor installed. Empty Redis/seed data remains degraded; no hosted fallback configured.'
