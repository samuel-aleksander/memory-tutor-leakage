"""Build a single-file transcript viewer for every toy run.

Reads data/toy/<run>/dialogues/*.json and writes data/toy/viewer.html, with all the data
embedded (no server needed: open the file in a browser). Rerun after each new run:

    python3 -I stage2/toy/build_viewer.py

The page has three views:
    Overview        max leakage level per (tutor prompt x memory), leakage by turn, run list
    Conversations   one dialogue at a time (or the three memory conditions side by side),
                    with the leakage check under every tutor turn; a blind mode hides the
                    tutor prompt and memory so you can read without knowing the condition
    Memory          the current paired histories from toy_config.py, in both formats, with
                    the instr and exec versions side by side and the latest manipulation
                    checks (check_histories.py, check_log_messages.py) next to them. A check
                    run on an older version of the text is shown as out of date.
The layout and scripts live in viewer_template.html next to this file.
"""

import ast
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE)]  # -I drops the script dir from sys.path

import histories  # noqa: E402
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

# Run options that change the system prompt besides the tutor prompt. Dialogues that differ
# in them get their own row in the overview ("restricted + log + memory instruction"), so
# results from different setups never share a cell. Runs from before an option existed
# didn't use it (summary memory, no memory instruction).
SETUP_LABELS = {"log": "log", "instr": "memory instruction"}


def setup_key(d):
    parts = [d.get("tutor_prompt", "?")]
    if d.get("memory", "summary") != "summary":
        parts.append(d["memory"])
    if d.get("memory_instruction"):
        parts.append("instr")
    return "+".join(parts)


def setup_label(key):
    base, *opts = key.split("+")
    return " + ".join([TUTOR_LABELS.get(base, base)] + [SETUP_LABELS.get(o, o) for o in opts])


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


# ---------------------------------------------------------------------------
# Disclosure judge (judge_disclosure.py): labels per tutor turn, if the run has been judged
# ---------------------------------------------------------------------------
DISC_FIELDS = ("label", "new", "n_lines", "n_new_lines", "disclosed_lines", "rationale",
               "what_is_new", "new_rationale", "ungrounded_lines", "error", "new_error")


def load_disclosure(run_dir, dialogue_file):
    """{turn number: judged turn} from <run>/disclosure/<dialogue>.json, or {} if not judged."""
    f = run_dir / "disclosure" / dialogue_file.name
    if not f.exists():
        return {}
    return {t["turn"]: t for t in json.loads(f.read_text())["turns"]}


def disc_summary(turns, disc):
    """Per-turn judge labels and new disclosed lines, aligned with the dialogue's turns."""
    if not disc:
        return {"judged": False}
    labels = [disc.get(t["turn"], {}).get("label") for t in turns]
    return {"judged": True, "disc_labels": labels,
            "disc_new": [bool(disc.get(t["turn"], {}).get("new")) for t in turns],
            "disc_lines": [disc.get(t["turn"], {}).get("n_new_lines") or 0 for t in turns]}


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
            disc = load_disclosure(run_dir, f)
            prompt, memory = split_system(d.get("system", ""))
            new_lines = new_lines_by_turn(d["turns"])
            run_ds.append({
                "id": f"{run_dir.name}/{f.stem}",
                "run": run_dir.name,
                "memory": d["condition"],
                "tutor": setup_key(d),
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
                **disc_summary(d["turns"], disc),
                "turns": [{
                    **{k: t.get(k) for k in TURN_FIELDS},
                    "raw_lines": t.get("leak", {}).get("n_code_lines"),
                    "leak": {k: t.get("leak", {}).get(k) for k in LEAK_FIELDS},
                    "stitch": {k: t.get("stitch", {}).get(k) for k in STITCH_FIELDS},
                    "disc": {k: disc[t["turn"]].get(k) for k in DISC_FIELDS} if t["turn"] in disc else None,
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


# ---------------------------------------------------------------------------
# Memory view: the paired histories and their latest checks
# ---------------------------------------------------------------------------
RATING_FIELDS = ("confidence", "programming_ability", "frustration", "politeness", "time_pressure")
EXPECTED_HELP_TYPE = {"instr": "understanding", "exec": "solutions", "neutral": "none"}


def latest(pattern):
    files = sorted(TOY_DIR.glob(pattern))  # names end in a sortable timestamp
    return files[-1] if files else None


def whole_log_check(memory):
    """Latest check_histories.py result for this format, summarized per version."""
    f = latest("manipulation_check_log_*.json" if memory == "log" else "manipulation_check_[0-9]*.json")
    if f is None:
        return None
    d = json.loads(f.read_text())
    rated = d.get("histories", {})
    fresh = bool(rated) and all(k in histories.available_kinds(memory)
                                and rated[k] == histories.build_memory(k, memory) for k in rated)
    out = {"file": f.name, "fresh": fresh, "versions": {}}
    for k in [k for k in histories.available_kinds(memory) if k in d["results"]]:
        # ratings outside 1-5 are rater errors (check_histories.py now rejects them)
        ok = [r for r in d["results"].get(k, []) if "error" not in r
              and all(isinstance(r.get(f), int) and 1 <= r[f] <= 5 for f in RATING_FIELDS)]
        out["versions"][k] = {
            "n": len(ok), "errors": len(d["results"].get(k, [])) - len(ok),
            "intended": sum(r.get("help_type") == EXPECTED_HELP_TYPE[k] for r in ok),
            **{field: round(statistics.mean(r[field] for r in ok), 1) if ok else None
               for field in RATING_FIELDS},
        }
    return out


def message_check():
    """Latest check_log_messages.py result: rubric labels per message id, if still current."""
    f = latest("log_message_check_*.json")
    if f is None:
        return None, {}
    d = json.loads(f.read_text())
    current = {}
    for kind in histories.KINDS:
        for i, j, m, text in histories.log_messages(kind):
            k = "both" if "both" in m else kind
            current[f"s{i}m{j}_{k}"] = text
    for i, j, m, text in histories.log_messages(histories.NEUTRAL):
        current[f"s{i}m{j}_neutral"] = text
    labels = {}
    for m in d["messages"]:
        counts = Counter(r.get("label", "error") for r in m["ratings"])
        labels[m["id"]] = {"expected": m["expected"], "n": len(m["ratings"]),
                           "counts": dict(counts.most_common()),
                           "fresh": current.get(m["id"]) == m["text"]}
    return {"file": f.name, "repeats": d.get("repeats")}, labels


def memory_data():
    """Both memory formats, structured for side-by-side reading."""
    log_meta, labels = message_check()
    log_sessions = []
    for i, s in enumerate(cfg.LOG_SESSIONS, 1):
        msgs = []
        for j, m in enumerate(s["messages"], 1):
            pastes = [{"kind": key, "text": f"```{lang}\n{m[key]}\n```"}
                      for key, lang in histories.PASTE_KEYS if key in m]
            if "both" in m:
                msgs.append({"id": f"s{i}m{j}", "pastes": pastes, "both": m["both"],
                             "label": labels.get(f"s{i}m{j}_both")})
            else:
                msgs.append({"id": f"s{i}m{j}", "pastes": pastes,
                             "form": histories.sentence_form(m["instr"]),
                             **{k: m[k] for k in histories.KINDS},
                             "labels": {k: labels.get(f"s{i}m{j}_{k}") for k in histories.KINDS}})
        log_sessions.append({"week": s["week"], "exercise": s["exercise"], "messages": msgs})
    neutral_sessions = [{"week": week, "exercise": exercise, "messages": [
        {"id": f"s{i}m{j}", "text": m[histories.NEUTRAL], "label": labels.get(f"s{i}m{j}_neutral"),
         "pastes": [{"kind": key, "text": f"```{lang}\n{m[key]}\n```"}
                    for key, lang in histories.PASTE_KEYS if key in m]}
        for j, m in enumerate(msgs, 1)]}
        for i, (week, exercise, msgs) in enumerate(histories.log_sessions(histories.NEUTRAL), 1)]
    summary_sessions = [{"week": s["week"], "exercise": s["exercise"],
                         "messages": [{"id": f"s{i}r{j}", **{k: r[k] for k in histories.KINDS}}
                                      for j, r in enumerate(s["requests"], 1)]}
                        for i, s in enumerate(cfg.SESSIONS, 1)]
    return {
        "default": cfg.MEMORY_FORMAT,
        "log": {"header": cfg.LOG_HEADER.strip(), "sessions": log_sessions,
                "text": {k: histories.build_memory(k, "log") for k in histories.available_kinds("log")},
                "neutral_sessions": neutral_sessions,
                "check": whole_log_check("log"), "message_check": log_meta},
        "summary": {"header": cfg.HISTORY_HEADER.strip(), "sessions": summary_sessions,
                    "text": {k: histories.build_memory(k, "summary") for k in histories.KINDS},
                    "check": whole_log_check("summary")},
    }


def main():
    runs, dialogues = load_runs()
    bases = list(cfg.TUTOR_PROMPTS)
    keys = {d["tutor"] for d in dialogues}
    tutor_order = sorted(keys, key=lambda k: (bases.index(k.split("+")[0]) if k.split("+")[0] in bases
                                              else len(bases), k))
    data = {"runs": runs, "dialogues": dialogues, "tutor_order": tutor_order,
            # only conditions with dialogues, so old runs don't get an empty D_neutral column
            "memory_order": [c for c in cfg.CONDITIONS if any(d["memory"] == c for d in dialogues)]
                            or list(cfg.CONDITIONS), "tutor_labels": {k: setup_label(k) for k in keys},
            "memories": memory_data()}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    OUT.write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", blob))
    print(f"{len(dialogues)} dialogues from {len(runs)} runs -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
