"""Per-task statistics for HumanEval, used to pick tasks long enough for a tutor to hint before solving.

Reads the raw HumanEval and HumanEval+ (EvalPlus, original format) files, measures each
canonical solution, and checks that the canonical solutions pass both test suites.

Usage (from the project root):
    python3 -I stage2/humaneval_stats.py
Writes data/processed/humaneval_task_stats.csv.
"""

import ast
import csv
import gzip
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HE_PATH = ROOT / "data/raw/humaneval/HumanEval.jsonl.gz"
PLUS_PATH = ROOT / "data/raw/humanevalplus/HumanEvalPlus-OriginFmt.jsonl.gz"
OUT_PATH = ROOT / "data/processed/humaneval_task_stats.csv"

TIMEOUT_S = 60


def load(path):
    with gzip.open(path, "rt") as f:
        return {row["task_id"]: row for row in map(json.loads, f)}


def sloc(code):
    """Lines that are neither blank nor comments."""
    return sum(1 for line in code.splitlines() if line.strip() and not line.strip().startswith("#"))


def structure(prompt, solution):
    """Statement count, control-flow count, helper functions and max nesting of the full function."""
    tree = ast.parse(prompt + solution)
    entry = [n for n in tree.body if isinstance(n, ast.FunctionDef)][-1]
    body = entry.body[1:] if isinstance(entry.body[0], ast.Expr) else entry.body  # drop docstring
    stmts = sum(isinstance(n, ast.stmt) for b in body for n in ast.walk(b))
    control = sum(
        isinstance(n, (ast.If, ast.For, ast.While, ast.comprehension, ast.IfExp, ast.Try))
        for b in body for n in ast.walk(b)
    )
    helpers = sum(isinstance(n, (ast.FunctionDef, ast.Lambda)) for b in body for n in ast.walk(b))
    # Helper functions defined in the prompt count too (e.g. tasks that ship a helper).
    helpers += len([n for n in tree.body if isinstance(n, ast.FunctionDef)]) - 1

    def depth(node, d=0):
        nested = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.FunctionDef)
        kids = [depth(c, d + isinstance(c, nested)) for c in ast.iter_child_nodes(node)]
        return max([d] + kids)

    return stmts, control, helpers, max(depth(b) for b in body)


def passes(prompt, solution, test, entry_point):
    program = f"{prompt}{solution}\n\n{test}\n\ncheck({entry_point})\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "prog.py")
        with open(path, "w") as f:
            f.write(program)
        try:
            r = subprocess.run([sys.executable, "-I", path], cwd=tmp, capture_output=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return "timeout"
    return "pass" if r.returncode == 0 else "fail"


def stats(task_id, he, plus):
    h, p = he[task_id], plus[task_id]
    stmts, control, helpers, nesting = structure(h["prompt"], h["canonical_solution"])
    docstring = ast.get_docstring([n for n in ast.parse(h["prompt"]).body if isinstance(n, ast.FunctionDef)][-1]) or ""
    return {
        "task_id": task_id,
        "entry_point": h["entry_point"],
        "sloc": sloc(h["canonical_solution"]),
        "sloc_plus": sloc(p["canonical_solution"]),
        "statements": stmts,
        "control_flow": control,
        "helpers": helpers,
        "max_nesting": nesting,
        "prompt_words": len(docstring.split()),
        "n_asserts": h["test"].count("assert"),
        "canonical_passes_he": passes(h["prompt"], h["canonical_solution"], h["test"], h["entry_point"]),
        "canonical_passes_plus": passes(p["prompt"], p["canonical_solution"], p["test"], p["entry_point"]),
        "he_canonical_passes_plus": passes(h["prompt"], h["canonical_solution"], p["test"], h["entry_point"]),
    }


def main():
    he, plus = load(HE_PATH), load(PLUS_PATH)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda t: stats(t, he, plus), he))
    rows.sort(key=lambda r: int(r["task_id"].split("/")[1]))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} tasks to {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
