"""Harness: the Python SDK's own consistent_hash and md5_hex.

Reads {"inputs": [[user_id, flag_key], ...]} on stdin and prints
{"hashes": [...], "md5_hex": [...]}; hash_contract.py does the comparing.
"""

import json
import sys
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[3] / "sdk" / "python"
sys.path.insert(0, str(SDK_ROOT))

import controlvar  # noqa: E402

# An `controlvar` wheel installed in this interpreter would also answer to
# `import controlvar`; make sure the one under test is the checkout's.
_loaded = Path(controlvar.__file__).resolve()
if SDK_ROOT / "controlvar" not in _loaded.parents:
    sys.exit(f"imported {_loaded}, not the SDK under {SDK_ROOT}")

inputs = json.load(sys.stdin)["inputs"]
json.dump(
    {
        "hashes": [controlvar.consistent_hash(u, f) for u, f in inputs],
        "md5_hex": [controlvar.md5_hex(u, f) for u, f in inputs],
    },
    sys.stdout,
)
