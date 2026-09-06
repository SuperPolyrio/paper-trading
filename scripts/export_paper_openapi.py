#!/usr/bin/env python3
"""Export or verify the checked-in Paper API OpenAPI document."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.api.routes.paper_v1 import build_openapi_spec

DEFAULT_OUTPUT = Path("docs/api/paper-v1-openapi.json")


def rendered_spec() -> str:
    return json.dumps(build_openapi_spec(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = rendered_spec()
    if args.check:
        if (
            not args.output.is_file()
            or args.output.read_text(encoding="utf-8") != expected
        ):
            raise SystemExit(f"OpenAPI artifact is stale: {args.output}")
        print(f"OpenAPI artifact is current: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(expected, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
