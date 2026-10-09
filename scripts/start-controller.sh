#!/usr/bin/env bash
set -euo pipefail

# Si sposta nella radice del progetto, ovunque venga lanciato lo script.
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

export PYTHONPATH="$PROJECT_ROOT/src"

exec "$PROJECT_ROOT/.venv/bin/ryu-manager" \
  --ofp-tcp-listen-port 6653 \
  src/controller/app.py