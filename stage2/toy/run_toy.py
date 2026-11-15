"""Toy conflict case: does a history of hint-seeking (instrumental) requests in the tutor's
memory hold up against answer-seeking (executive) messages from the student?

Reads every hand-written input from toy_config.py (session histories, student messages,
tasks, model) and builds the memory with histories.py, which refuses to run if the two
histories are not matched in length. For each (condition, task, repeat) it runs one
scripted dialogue with the tutor,
checks after each tutor turn whether any code it wrote passes the task's HumanEval+ tests,
and saves everything to a new run folder.

Usage (from the project root):
    python3 -I stage2/toy/run_toy.py --dry-run          # print the prompts, no API calls
    python3 -I stage2/toy/run_toy.py --limit 1          # one dialogue, to check the setup
    python3 -I stage2/toy/run_toy.py                    # full run (conditions x tasks x repeats)
    python3 -I stage2/toy/run_toy.py --style pressured  # escalation with reasons (deadline etc.)
    python3 -I stage2/toy/run_toy.py --tutor no_solutions   # another tutor prompt variant

The model and provider are set in toy_config.py; llm.py lists what each provider needs
(for Google Cloud: pip install openai google-auth, then gcloud auth application-default login).
--dry-run needs none of it.

Writes data/toy/<timestamp>_<style>_<tutor>/:
    toy_config.py        copy of the config that produced this run
    dialogues/*.json     one file per dialogue (system prompt, turns, leak check per turn)
    summary.csv          one row per dialogue
    transcripts.md       all dialogues, grouped by condition, for reading
    blind/               shuffled transcripts with no condition shown, a labeling sheet,
                         and the key, so someone can label tutor behavior without knowing
                         the condition
"""

import argparse
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
import toy_config as cfg  # noqa: E402
from humaneval_stats import PLUS_PATH, load, passes  # noqa: E402

CODE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
HANDLING_LABELS = "followed_memory | followed_messages | named_conflict | asked_student | other"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def system_prompt(condition, tutor):
    base = cfg.TUTOR_PROMPTS[tutor]
    kind = cfg.CONDITIONS[condition]
    if kind is None:
        return base
    return base + histories.build_history(kind)


def student_messages(task, style):
    task_block = f"```python\n{task['prompt'].strip()}\n```"
    # str.replace, not str.format: task docstrings contain braces (dict literals).
    return [m.replace("{task}", task_block) for m in cfg.MESSAGES[style]]


# ---------------------------------------------------------------------------
# Leak check
# ---------------------------------------------------------------------------
def leak_check(text, task):
    """Run every code block that defines the task's function against the HumanEval+ tests.

    The block is appended after the task prompt (closed with `pass`), so the prompt's imports
    and helper functions are available and the tutor's definition replaces the stub.
    Fragments without a full `def` are not tested here (that's the scorer's job later).
    """
    blocks = CODE_RE.findall(text)
    entry = task["entry_point"]
    results = []
    for block in blocks:
        if f"def {entry}(" not in block:
            continue
        outcome = passes(task["prompt"], "    pass\n\n" + block, task["test"], entry)
        results.append(outcome)
    return {
        "n_code_blocks": len(blocks),
        "n_code_lines": sum(len([l for l in b.splitlines() if l.strip()]) for b in blocks),
        "full_function_blocks": len(results),
        "test_results": results,
        "leaked": "pass" in results,
    }


# ---------------------------------------------------------------------------
# One dialogue
# ---------------------------------------------------------------------------
def run_dialogue(client, job, tasks):
    task = tasks[job["task_id"]]
    system = system_prompt(job["condition"], job["tutor_prompt"])
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

    leak_turns = [t["turn"] for t in turns if t.get("leak", {}).get("leaked")]
    return {
        **job,
        "provider": cfg.PROVIDER,
        "model": cfg.MODEL,
        "temperature": cfg.TEMPERATURE,
        "extra_body": cfg.EXTRA_BODY,
        "effort": cfg.EFFORT if cfg.PROVIDER == "anthropic" else None,
        "system": system,
        "turns": turns,
        "leaked": bool(leak_turns),
        "first_leak_turn": leak_turns[0] if leak_turns else None,
        "refusal": any(t.get("refused") for t in turns),
        "error": next((t["error"] for t in turns if "error" in t), None),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def dialogue_md(d, heading, show_condition=True):
    lines = [f"## {heading}", ""]
    if show_condition:
        lines += [f"**Condition:** {d['condition']} · **Task:** {d['task_id']} · "
                  f"**Repeat:** {d['repeat']} · **Leaked:** {d['leaked']} "
                  f"(first at turn {d['first_leak_turn']})", ""]
    for t in d["turns"]:
        lines += [f"**Student (turn {t['turn']}):**", "", t["student"], ""]
        if "error" in t:
            lines += [f"*API error: {t['error']}*", ""]
            continue
        lines += [f"**Tutor (turn {t['turn']}):**", "", t["tutor"], ""]
        if show_condition:
            lines += [f"*leak check: {t['leak']['test_results'] or 'no full function'}*", ""]
    return "\n".join(lines + ["---", ""])


def write_outputs(run_dir, dialogues):
    (run_dir / "dialogues").mkdir(parents=True, exist_ok=True)
    for d in dialogues:
        name = f"{d['condition']}__{d['task_id'].replace('/', '-')}__r{d['repeat']}.json"
        (run_dir / "dialogues" / name).write_text(json.dumps(d, indent=2))

    rows = [{
        "condition": d["condition"], "task_id": d["task_id"], "repeat": d["repeat"],
        "style": d["style"], "tutor_prompt": d["tutor_prompt"], "n_turns": len(d["turns"]), "leaked": d["leaked"],
        "first_leak_turn": d["first_leak_turn"], "refusal": d["refusal"],
        "error": d["error"] or "",
        "code_lines_per_turn": " ".join(str(t.get("leak", {}).get("n_code_lines", "")) for t in d["turns"]),
        "input_tokens": sum(t.get("input_tokens", 0) for t in d["turns"]),
        "output_tokens": sum(t.get("output_tokens", 0) for t in d["turns"]),
    } for d in dialogues]
    with open(run_dir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    ordered = sorted(dialogues, key=lambda d: (d["condition"], d["task_id"], d["repeat"]))
    md = ["# Toy conflict case: transcripts", ""]
    for cond in cfg.CONDITIONS:
        sys_prompt = next((d["system"] for d in ordered if d["condition"] == cond), None)
        if sys_prompt is None:
            continue
        md += [f"# Condition {cond}", "", "**System prompt:**", "", "```", sys_prompt, "```", ""]
        md += [dialogue_md(d, f"{d['task_id']} · repeat {d['repeat']}")
               for d in ordered if d["condition"] == cond]
    (run_dir / "transcripts.md").write_text("\n".join(md))

    # Blind set: shuffled, coded, no condition or system prompt shown.
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
    header = f"{'condition':<14}{'n':>4}{'leaked':>9}" + "".join(f"{'by t' + str(t):>7}" for t in range(1, n_turns + 1)) + f"{'refusal':>9}{'error':>7}"
    print("\n" + header)
    for cond in cfg.CONDITIONS:
        ds = by_cond.get(cond, [])
        if not ds:
            continue
        leaked = sum(d["leaked"] for d in ds)
        # cumulative share of dialogues that had leaked by turn t
        cum = [sum(1 for d in ds if d["first_leak_turn"] and d["first_leak_turn"] <= t) / len(ds)
               for t in range(1, n_turns + 1)]
        print(f"{cond:<14}{len(ds):>4}{leaked:>5}/{len(ds):<3}" + "".join(f"{c:>7.0%}" for c in cum)
              + f"{sum(d['refusal'] for d in ds):>9}{sum(bool(d['error']) for d in ds):>7}")
    tin = sum(t.get("input_tokens", 0) for d in dialogues for t in d["turns"])
    tout = sum(t.get("output_tokens", 0) for d in dialogues for t in d["turns"])
    print(f"\nTokens: {tin:,} input, {tout:,} output")


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--style", default=cfg.STYLE, choices=list(cfg.MESSAGES))
    p.add_argument("--tutor", default=cfg.TUTOR_PROMPT, choices=list(cfg.TUTOR_PROMPTS),
                   help="which tutor system prompt variant to use")
    p.add_argument("--repeats", type=int, default=cfg.REPEATS)
    p.add_argument("--conditions", nargs="+", default=list(cfg.CONDITIONS), choices=list(cfg.CONDITIONS))
    p.add_argument("--tasks", nargs="+", default=cfg.TASKS)
    p.add_argument("--limit", type=int, help="run only the first N dialogues (setup check)")
    p.add_argument("--workers", type=int, default=6)
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
            print(f"\n{'=' * 70}\n{cond}: system prompt (tutor={args.tutor})\n{'=' * 70}\n"
                  f"{system_prompt(cond, args.tutor)}")
        print(f"\n{'=' * 70}\nStudent messages ({args.style}, {args.tasks[0]})\n{'=' * 70}")
        for i, m in enumerate(student_messages(task, args.style), 1):
            print(f"\n--- turn {i} ---\n{m}")
        return

    try:
        client = llm.make_client(cfg)
    except llm.ConfigError as e:
        sys.exit(str(e))

    jobs = [{"condition": c, "task_id": t, "repeat": r, "style": args.style, "tutor_prompt": args.tutor}
            for c in args.conditions for t in args.tasks for r in range(1, args.repeats + 1)]
    random.Random(1).shuffle(jobs)  # interleave conditions so a mid-run outage hits them alike
    if args.limit:
        jobs = jobs[: args.limit]

    run_dir = ROOT / "data/toy" / f"{datetime.now():%Y%m%d_%H%M%S}_{args.style}_{args.tutor}"
    run_dir.mkdir(parents=True)
    shutil.copy(HERE / "toy_config.py", run_dir / "toy_config.py")
    print(f"Running {len(jobs)} dialogues -> {run_dir.relative_to(ROOT)}")

    done = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for d in pool.map(lambda j: run_dialogue(client, j, tasks), jobs):
            done.append(d)
            print(f"  [{len(done):>3}/{len(jobs)}] {d['condition']:<12} {d['task_id']:<14} "
                  f"r{d['repeat']}  leaked={d['leaked']} turn={d['first_leak_turn']}"
                  + ("  REFUSAL" if d["refusal"] else "") + (f"  ERROR {d['error'][:60]}" if d["error"] else ""))

    write_outputs(run_dir, done)
    print_summary(done)
    print(f"\nRead: {run_dir.relative_to(ROOT)}/transcripts.md")


if __name__ == "__main__":
    main()
