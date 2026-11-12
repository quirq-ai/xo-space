"""Enroll the browser fingerprint displayed at /founder. Never accepts the browser secret."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import founder_auth


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha256", help="The browser's SHA-256 fingerprint from /founder")
    args = parser.parse_args()
    try:
        created = founder_auth.enroll(args.sha256)
    except founder_auth.CredentialError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    print("Fingerprint installed. Connect the browser in the tab you kept open." if created else
          "This fingerprint is already installed. Connect the browser in its open tab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
