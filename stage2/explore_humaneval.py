"""Walk through one HumanEval task: print its columns, build the program, and run the tests.

Usage (from the project root):
    python3 -I stage2/explore_humaneval.py              # task HumanEval/0
    python3 -I stage2/explore_humaneval.py 10           # task HumanEval/10
    python3 -I stage2/explore_humaneval.py 0 --mine my_body.py
        # also test your own function body (indented 4 spaces, like canonical_solution)
    python3 -I stage2/explore_humaneval.py 0 --student
        # only what the simulated student is given: the problem, not the solution or tests
"""

import argparse
import gzip
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HE_PATH = ROOT / "data/raw/humaneval/HumanEval.jsonl.gz"


def banner(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def build_program(task, body):
    """prompt + body + test + check(entry_point): exactly how HumanEval is scored."""
    return f"{task['prompt']}{body}\n\n{task['test']}\n\ncheck({task['entry_point']})\n"


def run(program, timeout=10):
    """Run the program in a separate Python process; return (passed, last error line)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "prog.py")
        with open(path, "w") as f:
            f.write(program)
        try:
            r = subprocess.run([sys.executable, "-I", path], cwd=tmp, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s"
    if r.returncode == 0:
        return True, ""
    # The traceback names the failing line, e.g. "assert candidate([...], 0.3) == True".
    lines = [l.strip() for l in r.stderr.strip().splitlines() if l.strip()]
    failing = next((l for l in reversed(lines) if l.startswith("assert")), None)
    return False, failing or lines[-1]


def report(label, task, body):
    passed, why = run(build_program(task, body))
    print(f"{label}: {'PASS' if passed else 'FAIL'}")
    if not passed:
        print(f"    failed at: {why}")


def student_view(task):
    """The problem as the student gets it (the prompt column). The solution and the tests stay hidden."""
    banner(f"STUDENT VIEW: {task['task_id']}  (the prompt column)")
    print(task["prompt"])
    banner("HIDDEN FROM THE STUDENT")
    print("canonical_solution: the answer the tutor must not give away")
    print(f"test:               {task['test'].count('assert')} asserts, used afterwards to check whether")
    print("                    code the tutor wrote solves the task (= answer leakage)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", type=int, default=0, help="task number, 0-163")
    ap.add_argument("--mine", type=Path, help="file holding your own function body to test")
    ap.add_argument("--student", action="store_true", help="show only what the simulated student is given")
    args = ap.parse_args()

    with gzip.open(HE_PATH, "rt") as f:
        tasks = [json.loads(line) for line in f]
    task = tasks[args.task]

    if args.student:
        student_view(task)
        return

    for col in ["task_id", "entry_point", "prompt", "canonical_solution", "test"]:
        banner(f"COLUMN: {col}")
        print(task[col])

    banner("FULL PROGRAM THAT GETS RUN  (prompt + canonical_solution + test + check call)")
    print(build_program(task, task["canonical_solution"]))

    banner("RESULTS")
    report("canonical_solution        ", task, task["canonical_solution"])
    # A body that does nothing: the function returns None, so the first assert fails.
    report("empty body ('    pass')   ", task, "    pass\n")
    if args.mine:
        report(f"your body ({args.mine.name})", task, args.mine.read_text())


if __name__ == "__main__":
    main()
