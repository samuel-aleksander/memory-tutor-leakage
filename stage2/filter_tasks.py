"""Select the HumanEval tasks used in Stage 2: long enough, and structured enough, for a tutor to hint first.

Reads data/processed/humaneval_task_stats.csv (written by humaneval_stats.py) and keeps tasks that
  1. have at least MIN_SLOC lines of code (non-blank, non-comment) in BOTH the HumanEval and
     the HumanEval+ canonical solution,
  2. have at least MIN_CONTROL_FLOW control-flow constructs (if, for, while, comprehension,
     conditional expression, try), so long but straight-line solutions are dropped,
  3. can be scored: both canonical solutions pass their own test suites. This drops
     HumanEval/32 (find_zero), whose HumanEval+ test code is broken (it unpacks a float).

Usage (from the project root):
    python3 -I stage2/filter_tasks.py
Writes data/processed/humaneval_selected.csv and prints how many tasks each step removes.
"""

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATS_PATH = ROOT / "data/processed/humaneval_task_stats.csv"
OUT_PATH = ROOT / "data/processed/humaneval_selected.csv"

MIN_SLOC = 5
MIN_CONTROL_FLOW = 2

STEPS = [
    (f"at least {MIN_SLOC} lines in both canonical solutions",
     lambda r: min(int(r["sloc"]), int(r["sloc_plus"])) >= MIN_SLOC),
    (f"at least {MIN_CONTROL_FLOW} control-flow constructs",
     lambda r: int(r["control_flow"]) >= MIN_CONTROL_FLOW),
    ("both canonical solutions pass their own tests",
     lambda r: r["canonical_passes_he"] == "pass" and r["canonical_passes_plus"] == "pass"),
]

COLUMNS = ["task_id", "entry_point", "sloc", "sloc_plus", "control_flow", "max_nesting",
           "n_asserts", "weak_he_tests"]


def main():
    with open(STATS_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"{'all HumanEval tasks':<55}{len(rows):>4}")

    for label, keep in STEPS:
        dropped = [r["task_id"] for r in rows if not keep(r)]
        rows = [r for r in rows if keep(r)]
        print(f"{'+ ' + label:<55}{len(rows):>4}   (removed {len(dropped)})")
        if len(dropped) <= 6:
            print(f"    removed: {', '.join(dropped)}")

    for r in rows:
        # The original HumanEval solution fails or times out on the stricter HumanEval+ tests,
        # so the two test suites can disagree on whether tutor code is a working solution.
        r["weak_he_tests"] = r["he_canonical_passes_plus"] != "pass"

    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} tasks to {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
