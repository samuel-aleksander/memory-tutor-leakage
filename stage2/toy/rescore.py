"""Re-run the leak checks on saved dialogues, without calling the model again.

Use it when the leak measures change (e.g. after adding partial leakage): it reloads every
dialogue in a run folder, re-checks each tutor turn, and rewrites dialogues/, summary.csv,
transcripts.md and blind/ in place.

Usage (from the project root):
    python3 -I stage2/toy/rescore.py data/toy/<run folder> [<run folder> ...]

Note: blind/ is regenerated, so codes can change. Don't rescore a run whose blind labels
are already filled in.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # -I drops the script dir from sys.path

import run_toy  # noqa: E402
from humaneval_stats import PLUS_PATH, load  # noqa: E402


def rescore(run_dir, tasks):
    dialogues = []
    for path in sorted((run_dir / "dialogues").glob("*.json")):
        d = json.loads(path.read_text())
        for t in d["turns"]:
            if "tutor" in t:
                t["leak"] = run_toy.leak_check(t["tutor"], tasks[d["task_id"]])
        d.update(run_toy.leak_fields(d["turns"]))
        dialogues.append(d)
    if not dialogues:
        sys.exit(f"No dialogues in {run_dir}")
    run_toy.write_outputs(run_dir, dialogues)
    print(f"\n=== {run_dir}")
    run_toy.print_summary(dialogues)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tasks = load(PLUS_PATH)
    for arg in sys.argv[1:]:
        rescore(Path(arg).resolve(), tasks)


if __name__ == "__main__":
    main()
