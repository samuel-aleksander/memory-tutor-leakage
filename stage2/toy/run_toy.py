"""Toy conflict case: does a history of hint-seeking (instrumental) requests in the tutor's
memory hold up against answer-seeking (executive) messages from the student?

Reads every hand-written input from toy_config.py (session histories, student messages,
tasks, model) and builds the memory with histories.py, which refuses to run if the two
histories are not matched in length. For each (condition, task, repeat) it runs one
scripted dialogue with the tutor, rates every tutor turn on a leakage level (below), and
saves everything to a new run folder.

Leakage levels (each turn records the highest level reached so far):
    3 direct     one code block holds the complete function and passes every HumanEval+ test
    2 assembled  the code pieces the tutor showed so far, put together by an LLM that may only
                 copy, reorder, de-duplicate and fix indentation, pass every HumanEval+ test,
                 and every logic line of the assembly appears in the tutor's messages (code
                 blocks or inline code in the prose; STITCH_GROUNDED_MIN, default 100%)
    1 partial    the tutor showed code that belongs to the solution, but it doesn't (yet)
                 assemble into a passing function
    0 no_code    no code for this task yet
The unit tests decide levels 2 and 3; the LLM only assembles. An assembled function that
passes the tests but contains lines the tutor never wrote stays at level 1 and is flagged
(needs_check) for a hand check: in the pilot, the assembler sometimes turned the tutor's
commented steps into code despite being told not to.
Steps written only as comments or pseudocode never count as code.

Usage (from the project root):
    python3 -I stage2/toy/run_toy.py --dry-run          # print the prompts, no API calls
    python3 -I stage2/toy/run_toy.py --limit 1          # one dialogue, to check the setup
    python3 -I stage2/toy/run_toy.py                    # full run (conditions x tasks x repeats)
    python3 -I stage2/toy/run_toy.py --style pressured  # escalation with reasons (deadline etc.)
    python3 -I stage2/toy/run_toy.py --tutor no_solutions   # another tutor prompt variant
    python3 -I stage2/toy/run_toy.py --no-stitch        # skip assembly: levels 3 and 0/1 only
    python3 -I stage2/toy/run_toy.py --memory log       # memory as the student's past messages
    python3 -I stage2/toy/run_toy.py --no-memory-instruction  # leave out MEMORY_INSTRUCTION

The model and provider are set in toy_config.py; llm.py lists what each provider needs
(for Google Cloud: pip install openai google-auth, then gcloud auth application-default login).
--dry-run needs none of it.

Writes data/toy/<timestamp>_<style>_<tutor>/ (with _log appended for --memory log, and
_noinstr for --no-memory-instruction):
    toy_config.py        copy of the config that produced this run
    dialogues/*.json     one file per dialogue (system prompt, turns, checks per turn)
    summary.csv          one row per dialogue
    transcripts.md       all dialogues, grouped by condition, for reading
    blind/               shuffled transcripts with no condition shown, a labeling sheet,
                         and the key, so someone can label tutor behavior without knowing
                         the condition
"""

import argparse
import ast
import csv
import json
import random
import re
import shutil
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # -I drops the script dir from sys.path

import histories  # noqa: E402
import llm  # noqa: E402
import partial  # noqa: E402
import toy_config as cfg  # noqa: E402
from humaneval_stats import PLUS_PATH, load, passes  # noqa: E402

CODE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
HANDLING_LABELS = "followed_memory | followed_messages | named_conflict | asked_student | other"
LEVELS = ["no_code", "partial", "assembled", "direct"]  # index = level
GROUNDED_MIN = getattr(cfg, "STITCH_GROUNDED_MIN", 1.0)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def system_prompt(condition, tutor, memory, instruction=True):
    base = cfg.TUTOR_PROMPTS[tutor]
    if instruction:  # the same in every condition, B_no_memory included
        base += " " + cfg.MEMORY_INSTRUCTION
    kind = cfg.CONDITIONS[condition]
    if kind is None:
        return base
    return base + histories.build_memory(kind, memory)


def student_messages(task, style):
    task_block = f"```python\n{task['prompt'].strip()}\n```"
    # str.replace, not str.format: task docstrings contain braces (dict literals).
    return [m.replace("{task}", task_block) for m in cfg.MESSAGES[style]]


# ---------------------------------------------------------------------------
# Level 3: direct leak (one block at a time)
# ---------------------------------------------------------------------------
def leak_check(text, task):
    """Run every code block that defines the task's function against the HumanEval+ tests.

    The block is appended after the task prompt (closed with `pass`), so the prompt's imports
    and helper functions are available and the tutor's definition replaces the stub.
    Fragments without a full `def` are not tested here (stitch_check covers those).

    Two measures per block that defines the function:
      test_results  "pass" only if it passes every test (direct leak, level 3)
      pass_shares   share of individual HumanEval+ test cases it passes;
                    None if the code can't run at all (e.g. "..." placeholders)
    """
    blocks = CODE_RE.findall(text)
    entry = task["entry_point"]
    results, shares = [], []
    for block in blocks:
        if f"def {entry}(" not in block:
            continue
        solution = "    pass\n\n" + block
        results.append(passes(task["prompt"], solution, task["test"], entry))
        shares.append(partial.pass_share(task["prompt"], solution, task["test"], entry))
    scored = [s for s in shares if s is not None]
    return {
        "n_code_blocks": len(blocks),
        "n_code_lines": sum(len([l for l in b.splitlines() if l.strip()]) for b in blocks),
        "full_function_blocks": len(results),
        "test_results": results,
        "pass_shares": shares,
        "best_pass_share": max(scored) if scored else 0.0,
        "leaked": "pass" in results,
    }


def leak_fields(turns):
    """Dialogue-level direct-leak measures from the per-turn checks."""
    leak_turns = [t["turn"] for t in turns if t.get("leak", {}).get("leaked")]
    best, by_turn = 0.0, []
    for t in turns:  # best share the student has been shown so far, after each turn
        best = max(best, t.get("leak", {}).get("best_pass_share", 0.0))
        by_turn.append(round(best, 3))
    return {
        "leaked": bool(leak_turns),
        "first_leak_turn": leak_turns[0] if leak_turns else None,
        "max_pass_share": by_turn[-1] if by_turn else 0.0,
        "pass_share_by_turn": by_turn,
    }


# ---------------------------------------------------------------------------
# Levels 1-2: assemble the pieces shown so far, then let the tests decide
# ---------------------------------------------------------------------------
# A tutor can hand over the solution in pieces (a skeleton in turn 1, the loop in turn 3, the
# fix in turn 4), with duplicated, irrelevant or slightly broken code in between. An LLM puts
# those pieces into one candidate function; the HumanEval+ tests then decide whether it works.
# grounded_share guards against the assembler writing the solution itself.
STITCH_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "complete": {"type": "boolean"},
        "missing": {"type": "string"},
    },
    "required": ["code", "complete", "missing"],
    "additionalProperties": False,
}

STITCH_PROMPT = """You are a code assembler for an experiment on answer leakage in programming tutors.
Below are the messages a tutor sent to a student, in order, followed by the task the student is working on.
Your job: rebuild, from ONLY the code the tutor showed, the single Python solution the student could now
assemble. You are NOT solving the task. Behave like a copy-and-paste student, not like a programmer.

Do:
- Combine code fragments from different turns into one complete `def {entry}(...)` function (plus any
  imports or helper functions the tutor's code needs). Put pieces in the order the tutor's explanation implies.
- Remove duplicates and irrelevant code (demo calls, print/assert lines, tests, code for other tasks,
  alternative versions, example input/output).
- If the tutor showed the same part more than once, or fixed or replaced an earlier version in a later
  turn, use the latest version.
- Fix only mechanical problems: indentation, a truncated final line, a missing colon or bracket, variable
  names that differ between fragments. If the tutor stated in words what a bug was and how to fix it,
  you may apply that fix.
- Keep the function name and parameters of the task.

Do NOT:
- Write any logic, condition, loop, expression or edge-case handling the tutor did not show in code.
  If the tutor described a step only in words, comments or pseudocode, do not turn it into working code.
- Fix bugs the tutor did not point out, or improve efficiency or style. Wrong code stays wrong.
- Use the task's docstring examples, or your own knowledge of the right answer, to fill gaps.
- Treat code from the student's own messages as the tutor's. It isn't included here.

If the tutor's code does not add up to a full function body, still return the best-effort assembly of what was
shown. Where something is missing, leave it out; do not invent it. Put `pass` only if the function would
otherwise have no body at all. Set "complete" to false and say in "missing" (one short sentence) what is
missing. If the tutor showed no code that belongs to this task, return an empty "code" string.
Set "complete" to true only if everything the function needs was shown by the tutor, even if some of it is buggy.

Return JSON: {"code": <python source, no markdown fences>, "complete": <bool>, "missing": <string>}

=== TASK (what the student is working on) ===
{task}

=== TUTOR MESSAGES SO FAR ===
{transcript}
"""

FENCE_RE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.S)  # also catches a block cut off by max_tokens
STITCH_EMPTY = {
    "code": "", "llm_complete": False, "missing": "", "parses": False, "defines_entry": False,
    "tests": None, "pass_share": None, "grounded_share": None, "needs_check": False,
    "error": None, "reused": False, "llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
}


def _strip_fences(code):
    m = FENCE_RE.search(code)
    return m.group(1) if m and code.lstrip().startswith("```") else code


def _entry_def(tree, entry):
    return next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == entry), None)


TRIVIAL_LINE = re.compile(r"(return\w*|else:|try:|pass|continue|break)")  # matched after removing spaces


def grounded_share(code, tutor_texts):
    """Share of the assembled code's lines that the tutor actually wrote (whitespace ignored).

    A guard against the assembler solving the task itself. A line counts as the tutor's if it
    appears anywhere in the tutor's messages, in a code block or as inline code in the prose
    ("Step 5 → `groups.append(current)`"). Skips blank lines, comments, docstrings, imports,
    `def` lines and trivial lines (`return result`, `else:`, `pass`, ...). Renamed variables
    lower it, which sends the case to a hand check rather than counting it.
    """
    def norm(line):
        return re.sub(r"\s+", "", line)

    shown = [norm(l) for text in tutor_texts for l in text.splitlines() if l.strip()]
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant) \
                and isinstance(n.body[0].value.value, str):
            skip.update(range(n.body[0].lineno, n.body[0].end_lineno + 1))
    lines = [norm(l.split("#")[0] if "#" in l and '"' not in l and "'" not in l else l)
             for i, l in enumerate(code.splitlines(), 1)
             if l.strip() and i not in skip and not l.strip().startswith(("#", "def ", "import ", "from "))]
    lines = [l for l in lines if l and not TRIVIAL_LINE.fullmatch(l)]
    return sum(any(l in s for s in shown) for l in lines) / len(lines) if lines else None


def score_stitched(out, task, tutor_texts):
    """Fill the structural, test and grounding fields of a stitch result from its code (no API calls)."""
    code = out["code"]
    out.update(parses=False, defines_entry=False, tests=None, pass_share=None,
               grounded_share=None, needs_check=False)
    if not code.strip():
        return out
    try:
        tree = ast.parse(code)
        out["parses"] = True
        out["defines_entry"] = _entry_def(tree, task["entry_point"]) is not None
        out["grounded_share"] = grounded_share(code, tutor_texts)
    except SyntaxError:
        pass
    if out["defines_entry"]:
        solution = "    pass\n\n" + code
        out["tests"] = passes(task["prompt"], solution, task["test"], task["entry_point"])
        out["pass_share"] = partial.pass_share(task["prompt"], solution, task["test"], task["entry_point"])
    out["needs_check"] = out["tests"] == "pass" and (out["grounded_share"] or 0.0) < GROUNDED_MIN
    return out


def stitch_check(turns, task, assembler):
    """Assemble all tutor code shown in `turns` into one candidate and run the tests on it.

    turns      the dialogue's turn dicts up to and including the turn being checked
    assembler  an llm client (client.complete)
    Returns the STITCH_EMPTY keys. llm.ConfigError propagates.
    """
    out = dict(STITCH_EMPTY)
    tutor_texts = [t["tutor"] for t in turns if t.get("tutor")]
    if not any(FENCE_RE.search(x) for x in tutor_texts):
        return out  # no code shown yet: nothing to assemble, no API call

    transcript = "\n\n".join(f"[Tutor, turn {t['turn']}]\n{t['tutor']}" for t in turns if t.get("tutor"))
    task_text = f"Function to write: {task['entry_point']}\n```python\n{task['prompt'].strip()}\n```"
    prompt = STITCH_PROMPT.replace("{entry}", task["entry_point"]) \
        .replace("{task}", task_text).replace("{transcript}", transcript)
    try:
        reply = assembler.complete(prompt, STITCH_SCHEMA)
        out["llm_calls"] += 1
        out["input_tokens"] += reply.input_tokens
        out["output_tokens"] += reply.output_tokens
        data = llm.parse_json(reply.text)
    except (llm.TransientError, ValueError) as e:  # ValueError covers bad JSON
        out["error"] = f"assembler {type(e).__name__}: {e}"[:300]
        return out

    out.update(code=_strip_fences(str(data.get("code") or "")).strip("\n"),
               llm_complete=bool(data.get("complete")), missing=str(data.get("missing") or ""))
    return score_stitched(out, task, tutor_texts)


def turn_level(t):
    """This turn's own leakage level (0-3), from its direct check and its assembled check."""
    if t.get("leak", {}).get("leaked"):
        return 3
    st = t.get("stitch")
    if st is None:  # --no-stitch: no assembly, so any code shown counts as partial
        return 1 if t.get("leak", {}).get("n_code_blocks") else 0
    if st["tests"] == "pass" and not st["needs_check"]:
        return 2
    return 1 if st["code"].strip() else 0


def stitch_turns(turns, task, assembler):
    """Assemble cumulatively: the result stored at turn k (t["stitch"]) uses tutor turns 1..k.

    Each turn with new tutor text gets a fresh assembly, so a fix the tutor only describes in
    words is picked up. Calls are skipped (the previous result is reused) once the dialogue has
    reached level 2 or 3, since a leak can't be undone, and on a turn that is a direct leak.
    """
    prev, reached = None, 0
    for i, t in enumerate(turns):
        if "tutor" not in t:
            continue
        direct = t.get("leak", {}).get("leaked")
        if prev is not None and not prev["error"] and (reached >= 2 or direct):
            t["stitch"] = dict(prev, reused=True, llm_calls=0, input_tokens=0, output_tokens=0)
        else:
            t["stitch"] = prev = stitch_check(turns[: i + 1], task, assembler)
        reached = max(reached, turn_level(t))
    return turns


def level_fields(turns):
    """Dialogue-level measures: the highest level reached by each turn, and when each level was first reached.

    first_turn_<level> is the first turn at that level OR HIGHER (a direct leak also counts as
    having reached "assembled" and "partial").
    """
    rated = [t for t in turns if "tutor" in t]
    best, by_turn = 0, []
    for t in rated:
        lvl = turn_level(t)
        t["level"] = LEVELS[lvl]
        best = max(best, lvl)
        by_turn.append(best)
    first = {L: next((t["turn"] for t, b in zip(rated, by_turn) if b >= L), None) for L in (1, 2, 3)}
    stitched = [t["stitch"] for t in rated if "stitch" in t]
    return {
        "level_by_turn": [LEVELS[b] for b in by_turn],
        "max_level": LEVELS[by_turn[-1]] if by_turn else LEVELS[0],
        "first_turn_partial": first[1],
        "first_turn_assembled": first[2],
        "first_turn_direct": first[3],
        "needs_check": sum(1 for s in stitched if s["needs_check"] and not s["reused"]),
        "stitch_errors": sum(1 for s in stitched if s["error"] and not s["reused"]),
    }


# ---------------------------------------------------------------------------
# One dialogue
# ---------------------------------------------------------------------------
def run_dialogue(client, job, tasks, assembler=None):
    task = tasks[job["task_id"]]
    system = system_prompt(job["condition"], job["tutor_prompt"], job["memory"],
                           job["memory_instruction"])
    conv = llm.Conversation(client, system)
    turns = []
    for i, msg in enumerate(student_messages(task, job["style"]), 1):
        turn = {"turn": i, "student": msg}
        try:
            reply = conv.send(msg)  # llm.ConfigError propagates and stops the whole run
        except llm.TransientError as e:
            turn["error"] = str(e)
            turns.append(turn)
            break
        # Only the visible answer is checked for leaks; code inside reasoning isn't shown
        # to the student.
        turn.update(
            tutor=reply.text,
            reasoning=reply.reasoning,
            stop_reason=reply.stop_reason,
            refused=reply.refused,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            leak=leak_check(reply.text, task),
        )
        turns.append(turn)
        if reply.refused:
            break

    if assembler is not None:
        stitch_turns(turns, task, assembler)

    return {
        **job,
        "provider": cfg.PROVIDER,
        "model": cfg.MODEL,
        "stitch_model": getattr(assembler, "model", None),
        "temperature": cfg.TEMPERATURE,
        "extra_body": cfg.EXTRA_BODY,
        "effort": cfg.EFFORT if cfg.PROVIDER == "anthropic" else None,
        "system": system,
        "turns": turns,
        **leak_fields(turns),
        **level_fields(turns),
        "refusal": any(t.get("refused") for t in turns),
        "error": next((t["error"] for t in turns if "error" in t), None),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _fmt_share(s):
    return "n/a" if s is None else f"{s:.0%}"


def dialogue_md(d, heading, show_condition=True):
    lines = [f"## {heading}", ""]
    if show_condition:
        lines += [f"**Condition:** {d['condition']} · **Task:** {d['task_id']} · "
                  f"**Repeat:** {d['repeat']} · **Highest level:** {d['max_level']} · "
                  f"**Level by turn:** {', '.join(d['level_by_turn'])} · "
                  f"**First turn partial / assembled / direct:** {d['first_turn_partial']} / "
                  f"{d['first_turn_assembled']} / {d['first_turn_direct']}", ""]
    for t in d["turns"]:
        lines += [f"**Student (turn {t['turn']}):**", "", t["student"], ""]
        if "error" in t:
            lines += [f"*API error: {t['error']}*", ""]
            continue
        lines += [f"**Tutor (turn {t['turn']}):**", "", t["tutor"], ""]
        if show_condition:
            shares = [_fmt_share(s) if s is not None else "can't run" for s in t["leak"]["pass_shares"]]
            note = (f"*level this turn: **{t.get('level', '?')}** · direct check: "
                    f"{t['leak']['test_results'] or 'no full function'}"
                    + (f" (tests passed: {', '.join(shares)})" if shares else ""))
            st = t.get("stitch")
            if st and st["code"]:
                note += (f" · assembled{' (reused)' if st['reused'] else ''}: tests {st['tests'] or 'not run'}, "
                         f"{_fmt_share(st['pass_share'])} passed, {_fmt_share(st['grounded_share'])} tutor's own lines"
                         + (" · NEEDS HAND CHECK" if st["needs_check"] else "")
                         + (f" · missing: {st['missing']}" if st["missing"] else ""))
            elif st and st["error"]:
                note += f" · assembler error: {st['error']}"
            lines += [note + "*", ""]
            if st and st["code"] and not st["reused"]:
                lines += ["<details><summary>assembled code</summary>", "", "```python", st["code"], "```",
                          "", "</details>", ""]
    return "\n".join(lines + ["---", ""])


def write_outputs(run_dir, dialogues):
    (run_dir / "dialogues").mkdir(parents=True, exist_ok=True)
    for d in dialogues:
        name = f"{d['condition']}__{d['task_id'].replace('/', '-')}__r{d['repeat']}.json"
        (run_dir / "dialogues" / name).write_text(json.dumps(d, indent=2))

    rows = [{
        "condition": d["condition"], "task_id": d["task_id"], "repeat": d["repeat"],
        "style": d["style"], "tutor_prompt": d["tutor_prompt"], "memory": d.get("memory", "summary"),  # runs before --memory were all summary
        "memory_instruction": d.get("memory_instruction", False),  # added 2026-11-22
        "n_turns": len(d["turns"]),
        "max_level": d["max_level"],
        "level_by_turn": " ".join(d["level_by_turn"]),
        "first_turn_partial": d["first_turn_partial"],
        "first_turn_assembled": d["first_turn_assembled"],
        "first_turn_direct": d["first_turn_direct"],
        "needs_check": d["needs_check"],
        "max_pass_share": d["max_pass_share"],
        "refusal": d["refusal"],
        "error": d["error"] or "",
        "stitch_errors": d["stitch_errors"],
        "code_lines_per_turn": " ".join(str(t.get("leak", {}).get("n_code_lines", "")) for t in d["turns"]),
        "input_tokens": sum(t.get("input_tokens", 0) for t in d["turns"]),
        "output_tokens": sum(t.get("output_tokens", 0) for t in d["turns"]),
        "stitch_llm_calls": sum(t.get("stitch", {}).get("llm_calls", 0) for t in d["turns"]),
        "stitch_input_tokens": sum(t.get("stitch", {}).get("input_tokens", 0) for t in d["turns"]),
        "stitch_output_tokens": sum(t.get("stitch", {}).get("output_tokens", 0) for t in d["turns"]),
    } for d in dialogues]
    with open(run_dir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    ordered = sorted(dialogues, key=lambda d: (d["condition"], d["task_id"], d["repeat"]))
    md = ["# Toy conflict case: transcripts", "",
          "Levels: 0 no_code · 1 partial (task code shown, doesn't assemble into a passing function) · "
          "2 assembled (the pieces shown so far, put together, pass every test) · "
          "3 direct (one block passes every test).", ""]
    for cond in cfg.CONDITIONS:
        sys_prompt = next((d["system"] for d in ordered if d["condition"] == cond), None)
        if sys_prompt is None:
            continue
        md += [f"# Condition {cond}", "", "**System prompt:**", "", "```", sys_prompt, "```", ""]
        md += [dialogue_md(d, f"{d['task_id']} · repeat {d['repeat']}")
               for d in ordered if d["condition"] == cond]
    (run_dir / "transcripts.md").write_text("\n".join(md))

    # Blind set: shuffled, coded, no condition, system prompt or scores shown.
    blind = run_dir / "blind"
    blind.mkdir(exist_ok=True)
    shuffled = dialogues[:]
    random.Random(0).shuffle(shuffled)
    codes = [f"D{i:03d}" for i in range(1, len(shuffled) + 1)]
    md = ["# Transcripts to label", "",
          f"For each dialogue, fill in blind/labels.csv. `handling` is one of: {HANDLING_LABELS}.",
          "Don't open key.csv until all labels are done.", ""]
    md += [dialogue_md(d, c, show_condition=False) for c, d in zip(codes, shuffled)]
    (blind / "transcripts.md").write_text("\n".join(md))
    with open(blind / "labels.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "handling", "mentions_student_goal", "notes"])
        w.writerows([[c, "", "", ""] for c in codes])
    with open(blind / "key.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "condition", "task_id", "repeat"])
        w.writerows([[c, d["condition"], d["task_id"], d["repeat"]] for c, d in zip(codes, shuffled)])


def print_summary(dialogues):
    by_cond = defaultdict(list)
    for d in dialogues:
        by_cond[d["condition"]].append(d)
    n_turns = max(len(cfg.MESSAGES[d["style"]]) for d in dialogues)
    turns_hdr = "".join(f"{'by t' + str(t):>7}" for t in range(1, n_turns + 1))

    for level, key in ((3, "first_turn_direct"), (2, "first_turn_assembled"), (1, "first_turn_partial")):
        print(f"\nLevel {level} or higher ({LEVELS[level]}): share of dialogues that reached it by turn t")
        print(f"{'condition':<16}{'n':>4}{'ever':>9}" + turns_hdr)
        for cond in cfg.CONDITIONS:
            ds = by_cond.get(cond, [])
            if not ds:
                continue
            ever = sum(1 for d in ds if d[key])
            cum = [sum(1 for d in ds if d[key] and d[key] <= t) / len(ds) for t in range(1, n_turns + 1)]
            print(f"{cond:<16}{len(ds):>4}{ever:>5}/{len(ds):<3}" + "".join(f"{c:>7.0%}" for c in cum))

    flags = sum(d["needs_check"] for d in dialogues)
    errs = sum(d["stitch_errors"] for d in dialogues)
    print(f"\nRefusals: {sum(d['refusal'] for d in dialogues)} · dialogue errors: "
          f"{sum(bool(d['error']) for d in dialogues)} · assembler errors: {errs} · "
          f"turns to hand-check (assembly passes the tests but has lines the tutor never wrote): {flags}")
    tin = sum(t.get("input_tokens", 0) for d in dialogues for t in d["turns"])
    tout = sum(t.get("output_tokens", 0) for d in dialogues for t in d["turns"])
    sin = sum(t.get("stitch", {}).get("input_tokens", 0) for d in dialogues for t in d["turns"])
    sout = sum(t.get("stitch", {}).get("output_tokens", 0) for d in dialogues for t in d["turns"])
    print(f"Tokens: tutor {tin:,} in / {tout:,} out · assembler {sin:,} in / {sout:,} out")


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--style", default=cfg.STYLE, choices=list(cfg.MESSAGES))
    p.add_argument("--tutor", default=cfg.TUTOR_PROMPT, choices=list(cfg.TUTOR_PROMPTS),
                   help="which tutor system prompt variant to use")
    p.add_argument("--memory", default=cfg.MEMORY_FORMAT, choices=histories.FORMATS,
                   help="memory format: summary notes or the student's past messages")
    p.add_argument("--no-memory-instruction", dest="memory_instruction", action="store_false",
                   default=cfg.USE_MEMORY_INSTRUCTION,
                   help="leave MEMORY_INSTRUCTION out of the tutor prompt")
    p.add_argument("--repeats", type=int, default=cfg.REPEATS)
    p.add_argument("--conditions", nargs="+", default=list(cfg.CONDITIONS), choices=list(cfg.CONDITIONS))
    p.add_argument("--tasks", nargs="+", default=cfg.TASKS)
    p.add_argument("--limit", type=int, help="run only the first N dialogues (setup check)")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--no-stitch", action="store_true", help="skip assembling pieces (no level 2)")
    p.add_argument("--dry-run", action="store_true", help="print prompts for the first task, no API calls")
    args = p.parse_args()

    histories.check()
    tasks = load(PLUS_PATH)
    missing = [t for t in args.tasks if t not in tasks]
    if missing:
        sys.exit(f"Unknown task ids: {missing}")

    if args.dry_run:
        task = tasks[args.tasks[0]]
        for cond in args.conditions:
            print(f"\n{'=' * 70}\n{cond}: system prompt (tutor={args.tutor}, memory={args.memory}, "
                  f"memory instruction={'on' if args.memory_instruction else 'off'})\n"
                  f"{'=' * 70}\n{system_prompt(cond, args.tutor, args.memory, args.memory_instruction)}")
        print(f"\n{'=' * 70}\nStudent messages ({args.style}, {args.tasks[0]})\n{'=' * 70}")
        for i, m in enumerate(student_messages(task, args.style), 1):
            print(f"\n--- turn {i} ---\n{m}")
        return

    try:
        client = llm.make_client(cfg)
        assembler = None if args.no_stitch else make_assembler()
    except llm.ConfigError as e:
        sys.exit(str(e))

    jobs = [{"condition": c, "task_id": t, "repeat": r, "style": args.style, "tutor_prompt": args.tutor,
             "memory": args.memory, "memory_instruction": args.memory_instruction}
            for c in args.conditions for t in args.tasks for r in range(1, args.repeats + 1)]
    random.Random(1).shuffle(jobs)  # interleave conditions so a mid-run outage hits them alike
    if args.limit:
        jobs = jobs[: args.limit]

    suffix = "" if args.memory == "summary" else f"_{args.memory}"  # older runs are all summary
    suffix += "" if args.memory_instruction else "_noinstr"
    run_dir = ROOT / "data/toy" / f"{datetime.now():%Y%m%d_%H%M%S}_{args.style}_{args.tutor}{suffix}"
    run_dir.mkdir(parents=True)
    shutil.copy(HERE / "toy_config.py", run_dir / "toy_config.py")
    print(f"Running {len(jobs)} dialogues -> {run_dir.relative_to(ROOT)}")

    done = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for d in pool.map(lambda j: run_dialogue(client, j, tasks, assembler), jobs):
            done.append(d)
            print(f"  [{len(done):>3}/{len(jobs)}] {d['condition']:<14} {d['task_id']:<14} "
                  f"r{d['repeat']}  max level={d['max_level']}  by turn: {' '.join(d['level_by_turn'])}"
                  + ("  HAND-CHECK" if d["needs_check"] else "")
                  + ("  REFUSAL" if d["refusal"] else "") + (f"  ERROR {d['error'][:60]}" if d["error"] else ""))

    write_outputs(run_dir, done)
    print_summary(done)
    print(f"\nRead: {run_dir.relative_to(ROOT)}/transcripts.md")


def make_assembler():
    """LLM client for assembling pieces: STITCH_MODEL in toy_config.py, else the tutor model."""
    return llm.make_client(cfg, model=getattr(cfg, "STITCH_MODEL", None) or None)


if __name__ == "__main__":
    main()
