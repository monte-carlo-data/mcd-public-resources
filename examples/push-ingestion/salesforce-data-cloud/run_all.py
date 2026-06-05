#!/usr/bin/env python3
"""
Data 360 Full Lineage Push

One command to push complete end-to-end lineage for Salesforce Data 360:

  Step A  push_external_lineage.py   Source (CRM / Snowflake) → DLO
  Step B  push_lineage.py            DLO → DMO → CIO

Each script can also be run independently when you only need one hop.

Usage:
  python3 run_all.py               # full end-to-end push
  python3 run_all.py --dry-run     # preview every edge without committing
  python3 run_all.py --discover    # scan Data Stream connector types (Step A only)

Flags are forwarded to the individual scripts where they apply:
  --dry-run   passed to both scripts
  --discover  passed to push_external_lineage.py only; push_lineage.py is skipped
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

_STEP_A = ("Source → DLO  (push_external_lineage.py)", "push_external_lineage.py")
_STEP_B = ("DLO → DMO → CIO  (push_lineage.py)", "push_lineage.py")


def _run_step(label: str, script: str, extra_args: list) -> int:
    separator = "─" * 64
    print(f"\n{separator}", flush=True)
    print(f"  {label}", flush=True)
    print(f"{separator}", flush=True)
    result = subprocess.run(
        [sys.executable, str(HERE / script)] + extra_args,
        check=False,
    )
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Push complete Salesforce Data 360 lineage to Monte Carlo "
            "(Source→DLO then DLO→DMO→CIO)"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all steps but skip the MC push",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help=(
            "Scan Data Stream connector types and exit — "
            "runs push_external_lineage.py --discover only, no MC catalog or push"
        ),
    )
    args = parser.parse_args()

    base_args = ["--dry-run"] if args.dry_run else []

    # Step A: Source → DLO
    step_a_args = base_args + (["--discover"] if args.discover else [])
    rc = _run_step(*_STEP_A, step_a_args)
    if rc != 0:
        print(f"\n[run_all] Step A exited with code {rc} — stopping.", flush=True)
        sys.exit(rc)

    # Step B: DLO → DMO → CIO (skipped in --discover mode)
    if not args.discover:
        rc = _run_step(*_STEP_B, base_args)
        if rc != 0:
            print(f"\n[run_all] Step B exited with code {rc}.", flush=True)
            sys.exit(rc)

    print("\n[run_all] All lineage steps complete.", flush=True)


if __name__ == "__main__":
    main()
