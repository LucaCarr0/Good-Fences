#!/usr/bin/env bash
set -euo pipefail

# Avvia Open vSwitch nel container: il modulo kernel openvswitch è quello dell'host.
/usr/share/openvswitch/scripts/ovs-ctl start --system-id=random

# Un container riavviato conserva il database OVS: rimuove i bridge della run precedente.
/app/.venv/bin/python /app/scripts/cleanup-network.py

exec "$@"
