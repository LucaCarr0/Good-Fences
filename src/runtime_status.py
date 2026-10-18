"""Snapshot JSON atomici per i processi locali gestiti dal visualizzatore."""
import json
from pathlib import Path


def write_status(path, data):
    if path is None:
        return
    target = Path(path)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    temporary.replace(target)
