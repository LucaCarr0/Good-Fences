#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo '*** Pulizia precedenti run Good-Fences (controller Ryu preservato)...'
sudo "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/cleanup-network.py"

exec sudo "$PROJECT_ROOT/.venv/bin/python" src/datacenter.py \
  --controller remote \
  --controller-ip 127.0.0.1 \
  --controller-port 6653 \
  --cli
