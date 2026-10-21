"""Sign a strategy proposal so adaptive.py will accept it.

Usage:
    python council/sign_proposal.py '<json>'            # print signed JSON
    python council/sign_proposal.py '<json>' --write    # write to context/

The signature is an HMAC-SHA256 over the compact, key-sorted body using
COUNCIL_SECRET. It proves the council wrote the file; it is not a substitute
for the bounds, backtest, and revert gates, all of which still run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def sign(body: dict, secret: str) -> dict:
    """Return ``body`` with a valid signature attached."""
    body.pop("signature", None)
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    body["signature"] = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return body


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    try:
        body = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON: {exc}")
        return 1

    if "levers" not in body:
        print("Proposal must contain a 'levers' object.")
        return 1

    settings = config.load_settings()
    if not settings.council_secret:
        print("COUNCIL_SECRET is empty; signing is disabled and unnecessary.")
        return 1

    signed = sign(body, settings.council_secret)
    output = json.dumps(signed, indent=2)

    if "--write" in sys.argv:
        config.ensure_dirs()
        config.PROPOSAL_PATH.write_text(output)
        print(f"Wrote {config.PROPOSAL_PATH}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
