"""Fail when any single module drops below the coverage floor.

coverage.py only knows a global `fail_under`, and a global number hides the
case that matters: one module at 25% while the rest carry the average. That is
not hypothetical here - social, history, resources and play were all under 45%
while the total read a comfortable 72%, and nothing complained.

Run the suite with coverage first, then this:

    pytest --cov=src --cov-report=json
    python scripts/coverage_gate.py

It is a script rather than a test on purpose: as a test it would fail any time
someone ran a subset of the suite under coverage, which is a normal thing to do
and not a defect.
"""

import json
import pathlib
import sys

FLOOR = 85.0
REPORT = pathlib.Path("coverage.json")


def main() -> int:
    if not REPORT.exists():
        print(
            f"{REPORT} not found. Run: pytest --cov=src --cov-report=json",
            file=sys.stderr,
        )
        return 2

    report = json.loads(REPORT.read_text(encoding="utf-8"))
    files = report.get("files") or {}
    if not files:
        print("coverage.json contains no files", file=sys.stderr)
        return 2

    below = []
    for path, entry in sorted(files.items()):
        summary = entry.get("summary") or {}
        if not summary.get("num_statements"):
            continue
        percent = float(summary.get("percent_covered") or 0.0)
        if percent < FLOOR:
            below.append((path, percent))

    total = float((report.get("totals") or {}).get("percent_covered") or 0.0)
    if below:
        print(f"Below the {FLOOR:.0f}% floor (total {total:.0f}%):", file=sys.stderr)
        for path, percent in below:
            print(f"  {percent:5.1f}%  {path}", file=sys.stderr)
        return 1

    print(f"All {len(files)} modules at or above {FLOOR:.0f}% (total {total:.0f}%).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
