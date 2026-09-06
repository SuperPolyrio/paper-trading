#!/usr/bin/env python3
"""CLI entrypoint for the post-CLOB-V2 clean validation cohort."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.calibration.clean_v2_cohort_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
