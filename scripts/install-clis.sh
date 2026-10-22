#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# Optional CLI package bundle, separate from the app. Preserve normal proxy and CA trust.
npm ci --prefix tooling/cli --include=optional --ignore-scripts --no-audit --no-fund --cache .cache/npm-clis
# Run only the two official pinned wrappers' native binary-linking installers.
node tooling/cli/node_modules/@anthropic-ai/claude-code/install.cjs
node tooling/cli/node_modules/opencode-ai/postinstall.mjs
echo 'Installed the pinned optional npm CLI bundle. Existing logins and global installations were preserved.'
echo 'Use trading-team providers to inspect installation; use probe to verify the one selected CLI/model.'
