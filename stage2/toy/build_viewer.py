"""Build a single-file transcript viewer for every toy run.

Reads data/toy/<run>/dialogues/*.json and writes data/toy/viewer.html, with all the data
embedded (no server needed: open the file in a browser). Rerun after each new run:

    python3 -I stage2/toy/build_viewer.py

The page has two views:
    Overview        max leakage level per (tutor prompt x memory), leakage by turn, run list
    Conversations   one dialogue at a time (or the three memory conditions side by side),
                    with the leakage check under every tutor turn; a blind mode hides the
                    tutor prompt and memory so you can read without knowing the condition
The layout and scripts live in viewer_template.html next to this file.
"""

import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE)]  # -I drops the script dir from sys.path

import toy_config as cfg  # noqa: E402

TOY_DIR = ROOT / "data/toy"
OUT = TOY_DIR / "viewer.html"
TEMPLATE = HERE / "viewer_template.html"
MEMORY_MARKER = "\n\n## What you remember about this student"

# Display names for tutor prompts. Only the page uses these: run folders, dialogue files,
# toy_config.py and `--tutor` keep the code names on the left.
TUTOR_LABELS = {
    "open": "unrestricted",
    "no_solutions": "restricted",
    "zhao_adapted": "zhao-restricted",
}

# Run folders left out of the viewer, with the reason.
SKIP_RUNS = {
    "20261117_103001_plain_nudge": "setup check (--limit 1, default prompt), not a pilot",
}
# Single dialogues left out ("<run folder>/<dialogue file stem>"), with the reason. The files
# stay on disk, and the run's summary.csv still includes them.
SKIP_DIALOGUES = {
    "20261120_124722_plain_zhao_adapted/C_executive__HumanEval-1__r2":
        "model glitch: garbled tutor reply at turn 3 stayed in the history (turn 4 apologizes "
        "for it); replaced by a rerun of the same cell",
}

TURN_FIELDS = ("student", "tutor", "reasoning", "level", "refused")
LEAK_FIELDS = ("n_code_blocks", "full_function_blocks", "best_pass_share")
STITCH_FIELDS = ("code", "llm_complete", "missing", "tests", "pass_share", "grounded_share",
                 "needs_check", "error")


# ---------------------------------------------------------------------------
# Code handed out: new solution-logic lines per tutor turn
# ---------------------------------------------------------------------------
# A raw line count mostly measures how chatty the code blocks are, so this counts only
# distinct code lines the tutor had not shown before. Rule-based, no LLM:
#   counted   lines in fenced code blocks (a block cut off by max_tokens included),
#             control flow too (`continue`, `else:`, `return result`): it is part of the
#             solution
#   skipped   lines that hand out nothing (`pass`, `...`, a bare fill-in blank `___`),
#             blank lines, comments (whole-line and trailing), docstrings, imports, `def`
#             lines, doctest lines, lines that aren't valid Python (hand-traced
#             walkthroughs), and any line already in the student's first message (the
#             task stub)
#   repeats   a line shown again in a later turn (whitespace ignored) is not new
#   examples  a block that defines functions, none of them the task's, is a parallel
#             example (a different problem) and is skipped whole
# Limits: inline code in the prose (`groups.append(current)`) is not counted, and a helper
# function the tutor defines in a block of its own is skipped as if it were an example.
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.S)
DEF_RE = re.compile(r"^\s*def\s+(\w+)\s*\(", re.M)
# Lines that hand out nothing, matched after removing spaces. `_+` is a fill-in blank on its
# own line ("___"), valid Python (a name) but empty; a line that only contains a blank,
# like `if depth == ___:`, still counts. Unlike grounded_share in run_toy.py, control flow
# (`continue`, `else:`, `return result`) is not skipped here.
TRIVIAL_LINE = re.compile(r"(pass|\.\.\.|_+)")


def norm(line):
    return re.sub(r"\s+", "", line)


def strip_comment(s):
    """Drop a trailing # comment, leaving any # inside a string alone."""
    quote = None
    for i, c in enumerate(s):
        if quote:
            if c == quote and s[i - 1] != "\\":
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            return s[:i].rstrip()
    return s


def is_python(s):
    """True if the line is valid Python on its own, or as the header of a block.

    Keeps hand-traced walkthroughs out of the count ("depth = 0 + 1 = 1", "is depth == 0? No.").
    """
    candidates = [s]
    if s.endswith(":"):
        body = s + " pass"
        candidates += [body, "if 1: pass\n" + body, "try: pass\n" + body]
    for c in candidates:
        try:
            ast.parse(c)
            return True
        except SyntaxError:
            pass
    return False


def logic_lines(text, stub, task_defs):
    out, in_doc = [], False
    for block in FENCE_RE.findall(text or ""):
        defs = set(DEF_RE.findall(block))
        if defs and not defs & task_defs:
            continue
        for raw in block.splitlines():
            s = raw.strip()
            quotes = s.count('"""') + s.count("'''")
            if in_doc or quotes:
                in_doc = in_doc != (quotes % 2 == 1)
                continue
            s = strip_comment(s)
            n = norm(s)
            if (not n or s.startswith(("def ", "import ", "from ", ">>>")) or TRIVIAL_LINE.fullmatch(n)
                    or n in stub or not is_python(s)):
                continue
            out.append(n)
    return out


def new_lines_by_turn(turns):
    task_text = (turns[0].get("student") or "") if turns else ""
    stub = {norm(l) for l in task_text.splitlines() if l.strip()}
    task_defs = set(DEF_RE.findall(task_text))
    seen, counts = set(), []
    for t in turns:
        new = {l for l in logic_lines(t.get("tutor"), stub, task_defs) if l not in seen}
        seen |= new
        counts.append(len(new))
    return counts


def split_system(system):
    """System prompt -> (tutor prompt part, memory block or '')."""
    i = system.find(MEMORY_MARKER)
    return (system, "") if i < 0 else (system[:i], system[i:].strip())


def load_runs():
    runs, dialogues = [], []
    for run_dir in sorted(p for p in TOY_DIR.iterdir() if (p / "dialogues").is_dir()):
        if run_dir.name in SKIP_RUNS:
            print(f"skipped {run_dir.name}: {SKIP_RUNS[run_dir.name]}")
            continue
        files = []
        for f in sorted((run_dir / "dialogues").glob("*.json")):
            key = f"{run_dir.name}/{f.stem}"
            if key in SKIP_DIALOGUES:
                print(f"skipped {key}: {SKIP_DIALOGUES[key]}")
            else:
                files.append(f)
        if not files:
            continue
        run_ds = []
        for f in files:
            d = json.loads(f.read_text())
            prompt, memory = split_system(d.get("system", ""))
            new_lines = new_lines_by_turn(d["turns"])
            run_ds.append({
                "id": f"{run_dir.name}/{f.stem}",
                "run": run_dir.name,
                "memory": d["condition"],
                "tutor": d.get("tutor_prompt", "?"),
                "style": d.get("style", "?"),
                "task": d["task_id"],
                "repeat": d["repeat"],
                "model": d.get("model", ""),
                "prompt": prompt,
                "memory_text": memory,
                "max_level": d.get("max_level"),
                "levels": d.get("level_by_turn", []),
                "needs_check": d.get("needs_check", 0),
                "error": d.get("error"),
                "new_lines": new_lines,
                "turns": [{
                    **{k: t.get(k) for k in TURN_FIELDS},
                    "raw_lines": t.get("leak", {}).get("n_code_lines"),
                    "leak": {k: t.get("leak", {}).get(k) for k in LEAK_FIELDS},
                    "stitch": {k: t.get("stitch", {}).get(k) for k in STITCH_FIELDS},
                } for t in d["turns"]],
            })
        dialogues += run_ds
        first = run_ds[0]
        runs.append({"id": run_dir.name, "style": first["style"], "tutor": first["tutor"],
                     "model": first["model"], "n": len(run_ds),
                     "skipped": sorted(SKIP_DIALOGUES[k] for k in SKIP_DIALOGUES
                                       if k.startswith(run_dir.name + "/")),
                     "needs_check": sum(d["needs_check"] for d in run_ds),
                     "errors": sum(1 for d in run_ds if d["error"])})
    return runs, dialogues


def main():
    runs, dialogues = load_runs()
    tutor_order = list(cfg.TUTOR_PROMPTS)
    tutor_order += sorted({d["tutor"] for d in dialogues} - set(tutor_order))
    data = {"runs": runs, "dialogues": dialogues, "tutor_order": tutor_order,
            "memory_order": list(cfg.CONDITIONS), "tutor_labels": TUTOR_LABELS}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    OUT.write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", blob))
    print(f"{len(dialogues)} dialogues from {len(runs)} runs -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
