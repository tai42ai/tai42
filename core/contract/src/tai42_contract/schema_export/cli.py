"""``tai42-contract-schemas`` — emit or freshness-check the served-document bundle.

``--out PATH`` writes the bundle; ``--check`` rebuilds it in memory and exits non-zero
when it differs from the committed copy, naming every document (and the version) that
drifted so the fix is obvious. With neither flag the bundle is written to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tai42_contract.schema_export.registry import (
    build_document_schemas,
    bundle_drift,
    bundle_json,
    committed_bundle_path,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", metavar="PATH", help="Write the bundle to PATH instead of stdout.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Rebuild the bundle and exit non-zero if it differs from the committed copy; write nothing.",
    )
    args = parser.parse_args(argv)

    fresh = build_document_schemas()

    if args.check:
        committed_path = committed_bundle_path()
        committed = json.loads(committed_path.read_text(encoding="utf-8"))
        drift = bundle_drift(fresh, committed)
        if not drift:
            print(f"served-document bundle is fresh ({committed_path}).", file=sys.stderr)
            return 0
        print(f"served-document bundle at {committed_path} is stale:", file=sys.stderr)
        for line in drift:
            print(f"  - {line}", file=sys.stderr)
        print("regenerate with: tai42-contract-schemas --out <path>", file=sys.stderr)
        return 1

    text = bundle_json(fresh)
    if args.out is None:
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote served-document bundle to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
