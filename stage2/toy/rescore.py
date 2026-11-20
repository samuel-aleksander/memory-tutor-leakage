"""Re-apply the leakage checks to saved dialogues, without asking the tutor anything again.

It reloads every dialogue in a run folder, re-runs the direct check on each tutor turn,
re-runs the tests on any assembled code already saved, recomputes the leakage levels, and
rewrites dialogues/, summary.csv, transcripts.md and blind/ in place.

With --stitch it also (re)assembles the tutor's code pieces with the LLM (API calls: at
most one per tutor turn), e.g. for runs made before assembly existed.

Usage (from the project root):
    python3 -I stage2/toy/rescore.py data/toy/<run folder> [<run folder> ...]
    python3 -I stage2/toy/rescore.py --stitch data/toy/<run folder>

Note: blind/ is regenerated, so codes can change. Don't rescore a run whose blind labels
are already filled in.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # -I drops the script dir from sys.path

import llm  # noqa: E402
import run_toy  # noqa: E402
from humaneval_stats import PLUS_PATH, load  # noqa: E402


def rescore(run_dir, tasks, assembler=None):
    dialogues = []
    for path in sorted((run_dir / "dialogues").glob("*.json")):
        d = json.loads(path.read_text())
        task = tasks[d["task_id"]]
        for i, t in enumerate(d["turns"]):
            if "tutor" not in t:
                continue
            t["leak"] = run_toy.leak_check(t["tutor"], task)
            if assembler is None and "stitch" in t:  # keep the saved assembly, redo its tests
                texts = [x["tutor"] for x in d["turns"][: i + 1] if x.get("tutor")]
                t["stitch"] = {**run_toy.STITCH_EMPTY, **t["stitch"]}
                run_toy.score_stitched(t["stitch"], task, texts)
        if assembler is not None:
            for t in d["turns"]:
                t.pop("stitch", None)
            run_toy.stitch_turns(d["turns"], task, assembler)
            d["stitch_model"] = assembler.model
        for old in ("stitch_leaked", "stitch_first_leak_turn", "stitch_best_verdict", "stitch_best_by_turn"):
            d.pop(old, None)  # fields from the earlier LLM-judge version
        d.update(run_toy.leak_fields(d["turns"]))
        d.update(run_toy.level_fields(d["turns"]))
        dialogues.append(d)
    if not dialogues:
        sys.exit(f"No dialogues in {run_dir}")
    run_toy.write_outputs(run_dir, dialogues)
    print(f"\n=== {run_dir}")
    run_toy.print_summary(dialogues)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("runs", nargs="+", help="run folders under data/toy/")
    p.add_argument("--stitch", action="store_true", help="(re)assemble code pieces with the LLM (API calls)")
    args = p.parse_args()
    tasks = load(PLUS_PATH)
    assembler = None
    if args.stitch:
        try:
            assembler = run_toy.make_assembler()
        except llm.ConfigError as e:
            sys.exit(str(e))
    for arg in args.runs:
        rescore(Path(arg).resolve(), tasks, assembler)


if __name__ == "__main__":
    main()
