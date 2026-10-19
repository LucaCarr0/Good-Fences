#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

exec sudo --preserve-env=DISPLAY,XAUTHORITY "$PROJECT_ROOT/.venv/bin/python" -m streamlit run \
  "$PROJECT_ROOT/visualizer.py" \
  --server.address 127.0.0.1 \
  --server.headless true \
  --browser.gatherUsageStats false
