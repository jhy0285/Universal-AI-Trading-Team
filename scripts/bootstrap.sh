#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export UV_CACHE_DIR="$PWD/.cache/uv"
uv sync --frozen
.venv/bin/python - <<'PY'
import os
import secrets
from pathlib import Path

path = Path('.env')
if not path.exists():
    text = Path('.env.example').read_text()
    text = text.replace('API_ADMIN_TOKEN=\n', 'API_ADMIN_TOKEN=' + secrets.token_urlsafe(32) + '\n')
    text += '\nPOSTGRES_PASSWORD=' + secrets.token_urlsafe(32) + '\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(text)
    print('Created private .env with local administration/database tokens; no LLM credentials added.')
else:
    print('Preserving existing .env.')
PY
.venv/bin/trading-team init
