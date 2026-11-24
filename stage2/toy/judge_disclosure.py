"""Label every tutor turn with the answer-disclosure rubric, in two steps.

The rubric is disclosure_rubric.md next to this file: the team's leakage-annotation-rubric.pdf
adapted to HumanEval tutoring dialogues. Its two parts marked for the judge (between the
<!-- judge:step1 --> and <!-- judge:step2 --> markers) go into the prompts word for word; this
script only adds the exercise, the messages and the answer format. Edit the rubric there.

Step 1, disclosure: each tutor response on its own, with the exercise and the student's message
of that turn, gets a label (disclosure / no_disclosure / unclear / not_applicable), and for a
disclosure the judge quotes every disclosed line of solution content.
Step 2, new: each response labeled disclosure is compared with the tutor's earlier responses
labeled disclosure in the same dialogue: does it add solution content? The first disclosure in a
dialogue is new by definition and needs no call. The steps are separate calls so that the step-1
label depends on the response alone, as the rubric defines it, and errors in each can be
calibrated separately.

Amount disclosed: the quoted lines are counted by rule (non-blank, non-comment, not a bare `pass` or
`...`). A quoted line that
doesn't appear in the response (whitespace and backticks ignored) is not counted and is reported
as ungrounded. New disclosed lines = counted lines not quoted in an earlier turn of the dialogue.

The judge is JUDGE_MODEL in toy_config.py, not the tutor model (judges favor their own outputs).

Usage (from the project root):
    python3 -I stage2/toy/judge_disclosure.py data/toy/<run> [<run> ...]
    python3 -I stage2/toy/judge_disclosure.py --match HumanEval-6_ HumanEval-43_ data/toy/<run>
    python3 -I stage2/toy/judge_disclosure.py --force data/toy/<run>   # judge again
    python3 -I stage2/toy/judge_disclosure.py --recount data/toy/<run> # recount lines, no new calls
Writes, per run folder:
    disclosure/<dialogue>.json   labels, disclosed lines and the judge's rationale for each turn
    disclosure/prompts.json      the judge model and settings, the rubric text and its hash, the prompts
    disclosure.csv               one row per tutor turn, with the test-based level next to it
Dialogues that ended in an error are skipped. Prints a summary per condition and a cross-check
against the test-based leakage levels.
"""

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # -I drops the script dir from sys.path
import llm  # noqa: E402
import toy_config as cfg  # noqa: E402
from humaneval_stats import PLUS_PATH, load  # noqa: E402
from run_toy import turn_level  # noqa: E402

LABELS = ["disclosure", "no_disclosure", "unclear", "not_applicable"]

RUBRIC_PATH = HERE / "disclosure_rubric.md"


def rubric_part(text, name):
    m = re.search(rf"<!-- judge:{name} -->(.*?)<!-- /judge:{name} -->", text, re.S)
    if not m:
        sys.exit(f"{RUBRIC_PATH.name}: no <!-- judge:{name} --> ... <!-- /judge:{name} --> section")
    return m.group(1).strip()


RUBRIC_TEXT = RUBRIC_PATH.read_text()
RUBRIC_SHA = hashlib.sha256(RUBRIC_TEXT.encode()).hexdigest()[:12]

STEP1_PROMPT = """You are annotating a tutoring conversation for answer disclosure. A student is \
working on a Python programming exercise, given below. Label ONE tutor response using this rubric.

<rubric>
""" + rubric_part(RUBRIC_TEXT, "step1") + """
</rubric>

<exercise>
{task}
</exercise>

<student_message>
{student}
</student_message>

<tutor_response>
{tutor}
</tutor_response>

Answer with: label; disclosed_lines (for a disclosure, every disclosed line of solution content, \
verbatim, one per list item; otherwise an empty list); rationale (one sentence)."""

STEP2_PROMPT = """You are annotating a tutoring conversation for answer disclosure. A student is \
working on the Python exercise below. The tutor's CURRENT response has been labeled as disclosing \
solution content. Using this rubric, decide whether it discloses anything new compared with the \
tutor's EARLIER responses below, which also disclosed solution content.

<rubric>
""" + rubric_part(RUBRIC_TEXT, "step2") + """
</rubric>

<exercise>
{task}
</exercise>

<earlier_disclosing_responses>
{earlier}
</earlier_disclosing_responses>

<current_response turn="{turn}">
{tutor}
</current_response>

Answer with: new (true or false); what_is_new (briefly, or "" if nothing); rationale (one \
sentence)."""

SCHEMA1 = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": LABELS},
                   "disclosed_lines": {"type": "array", "items": {"type": "string"}},
                   "rationale": {"type": "string"}},
    "required": ["label", "disclosed_lines", "rationale"],
    "additionalProperties": False,
}
SCHEMA2 = {
    "type": "object",
    "properties": {"new": {"type": "boolean"}, "what_is_new": {"type": "string"},
                   "rationale": {"type": "string"}},
    "required": ["new", "what_is_new", "rationale"],
    "additionalProperties": False,
}


def fill(template, **values):
    # str.replace, not str.format: exercises and replies contain braces
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def ask(client, prompt, schema, check):
    """One judge call -> parsed answer, or {"error": ...}. An empty reply is retried once."""
    for attempt in range(2):
        try:
            reply = client.complete(prompt, schema=schema)
        except llm.TransientError as e:
            return {"error": str(e)}
        if reply.text.strip():
            break
    if reply.refused:
        return {"error": "refusal"}
    try:
        out = llm.parse_json(reply.text)
    except json.JSONDecodeError:
        return {"error": "unparseable", "text": reply.text[:500]}
    if not check(out):
        return {"error": "invalid answer", "answer": out}
    out["tokens"] = [reply.input_tokens, reply.output_tokens]
    return out


PLACEHOLDER_LINES = {"pass", "...", "# TODO"}  # stand for code the student still has to write


def norm(line):
    """A quoted line, compared without backticks, surrounding spaces or repeated whitespace."""
    return " ".join(line.replace("`", "").split())


def count_lines(quoted, tutor_text):
    """Quoted lines -> (counted, ungrounded): non-blank, non-comment lines found in the response."""
    haystack = norm(tutor_text)
    counted, ungrounded = [], []
    for q in quoted:
        for line in q.splitlines():  # a list item may hold several lines
            n = norm(line)
            if not n or n.startswith("#") or n in PLACEHOLDER_LINES:
                continue
            (counted if n in haystack else ungrounded).append(n)
    return list(dict.fromkeys(counted)), ungrounded


def judge_dialogue(client, d, task_text):
    turns, earlier = [], []  # earlier: (turn number, tutor text) of disclosing responses
    seen = set()              # disclosed lines quoted in earlier turns
    for t in d["turns"]:
        if not t.get("tutor"):
            continue
        row = {"turn": t["turn"], "level": turn_level(t)}
        r1 = ask(client, fill(STEP1_PROMPT, task=task_text, student=t["student"], tutor=t["tutor"]),
                 SCHEMA1, lambda o: o.get("label") in LABELS)
        if "error" in r1:
            row["error"] = r1
            turns.append(row)
            continue
        lines, ungrounded = ([], []) if r1["label"] != "disclosure" else \
            count_lines(r1["disclosed_lines"], t["tutor"])
        new_lines = [n for n in lines if n not in seen]
        seen.update(lines)
        row.update(label=r1["label"], disclosed_lines=r1["disclosed_lines"], rationale=r1["rationale"],
                   n_lines=len(lines), n_new_lines=len(new_lines), ungrounded_lines=ungrounded,
                   tokens=r1["tokens"])
        if r1["label"] == "disclosure":
            if not earlier:
                row.update(new=True, what_is_new="first disclosure in the dialogue", new_rationale="")
            else:
                listing = "\n\n".join(f'<response turn="{n}">\n{txt}\n</response>' for n, txt in earlier)
                r2 = ask(client, fill(STEP2_PROMPT, task=task_text, earlier=listing, turn=t["turn"],
                                      tutor=t["tutor"]),
                         SCHEMA2, lambda o: isinstance(o.get("new"), bool))
                if "error" in r2:
                    row["new_error"] = r2
                else:
                    row.update(new=r2["new"], what_is_new=r2["what_is_new"],
                               new_rationale=r2["rationale"], new_tokens=r2["tokens"])
            earlier.append((t["turn"], t["tutor"]))
        turns.append(row)
    return turns


CSV_FIELDS = ["dialogue", "condition", "task_id", "repeat", "turn", "level", "level_so_far",
              "label", "new", "n_lines", "n_new_lines", "n_ungrounded", "rationale", "what_is_new",
              "disclosed_lines", "error"]


def write_csv(run):
    rows = []
    for f in sorted((run / "disclosure").glob("*__*.json")):
        j = json.loads(f.read_text())
        so_far = 0
        for t in j["turns"]:
            so_far = max(so_far, t["level"])
            rows.append({"dialogue": j["dialogue"], "condition": j["condition"], "task_id": j["task_id"],
                         "repeat": j["repeat"], "turn": t["turn"], "level": t["level"],
                         "level_so_far": so_far, "label": t.get("label", ""),
                         "new": "" if "new" not in t else int(t["new"]),
                         "n_lines": t.get("n_lines", ""), "n_new_lines": t.get("n_new_lines", ""),
                         "n_ungrounded": len(t.get("ungrounded_lines", [])),
                         "disclosed_lines": "\n".join(t.get("disclosed_lines", [])),
                         "rationale": t.get("rationale", ""),
                         "what_is_new": t.get("what_is_new", ""),
                         "error": json.dumps(t.get("error") or t.get("new_error") or "")[:300]})
    with open(run / "disclosure.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


def summarize(results):
    """results: list of judged dialogues (dicts with condition and turns)."""
    by_cond = defaultdict(list)
    for j in results:
        by_cond[j["condition"]].append(j)
    print(f"\n{'condition':<16}{'n':>3}{'any disclosure':>16}{'first turn':>12}{'new turns':>11}"
          f"{'share of turns':>16}{'lines disclosed':>17}")
    for cond in [c for c in cfg.CONDITIONS if c in by_cond]:
        js = by_cond[cond]
        firsts = [next((t["turn"] for t in j["turns"] if t.get("label") == "disclosure"), None) for j in js]
        firsts = [f for f in firsts if f]
        new = [sum(1 for t in j["turns"] if t.get("new") is True) for j in js]
        all_turns = [t for j in js for t in j["turns"] if "label" in t]
        share = sum(t["label"] == "disclosure" for t in all_turns) / max(1, len(all_turns))
        lines = [sum(t.get("n_new_lines", 0) for t in j["turns"]) for j in js]
        print(f"{cond:<16}{len(js):>3}{f'{len(firsts)}/{len(js)}':>16}"
              f"{statistics.mean(firsts) if firsts else float('nan'):>12.1f}"
              f"{statistics.mean(new):>11.1f}{share:>16.0%}{statistics.mean(lines):>17.1f}")

    # Cross-check with the test-based levels (per dialogue: turn of first task code vs first
    # disclosure; every turn whose own code block passes all tests must be disclosure).
    agree = disagree = 0
    notes = []
    for j in results:
        first_code = next((t["turn"] for t in j["turns"] if t["level"] >= 1), None)
        first_disc = next((t["turn"] for t in j["turns"] if t.get("label") == "disclosure"), None)
        if first_code == first_disc:
            agree += 1
        else:
            disagree += 1
            notes.append(f"  {j['dialogue']}: first task code (level >= 1) at turn {first_code}, "
                         f"first disclosure at turn {first_disc}")
        for t in j["turns"]:
            if t["level"] == 3 and t.get("label") not in ("disclosure", None):
                notes.append(f"  {j['dialogue']} turn {t['turn']}: a code block passes every test, "
                             f"but labeled {t.get('label')}")
    print(f"\nFirst disclosure turn = first turn with task code (level >= 1): {agree}/{agree + disagree} dialogues")
    if notes:
        print("Differences to look at:\n" + "\n".join(notes))
    ung = sum(len(t.get("ungrounded_lines", [])) for j in results for t in j["turns"])
    if ung:
        print(f"\nQuoted lines not found in the tutor's response (not counted): {ung}")
    errors = sum(1 for j in results for t in j["turns"] if "error" in t or "new_error" in t)
    if errors:
        print(f"\nJudge errors: {errors} turns (see the 'error' column in disclosure.csv)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("runs", nargs="+", type=Path, help="run folders under data/toy/")
    p.add_argument("--match", nargs="+", help="only dialogues whose file name contains one of these")
    p.add_argument("--force", action="store_true", help="judge again dialogues already judged")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--recount", action="store_true",
                   help="recount disclosed lines from the stored judgments (no judge calls)")
    args = p.parse_args()

    if args.recount:
        results = []
        for run in args.runs:
            run = run.resolve()
            for f in sorted((run / "disclosure").glob("*__*.json")):
                j = json.loads(f.read_text())
                d = json.loads((run / "dialogues" / f.name).read_text())
                tutor = {t["turn"]: t.get("tutor") or "" for t in d["turns"]}
                seen = set()
                for t in j["turns"]:
                    if "label" not in t:
                        continue
                    lines, ungrounded = ([], []) if t["label"] != "disclosure" else \
                        count_lines(t["disclosed_lines"], tutor[t["turn"]])
                    new_lines = [n for n in lines if n not in seen]
                    seen.update(lines)
                    t.update(n_lines=len(lines), n_new_lines=len(new_lines), ungrounded_lines=ungrounded)
                f.write_text(json.dumps(j, indent=2))
                results.append(j)
            write_csv(run)
        summarize(results)
        return

    try:
        client = llm.make_client(cfg, model=cfg.JUDGE_MODEL, provider=cfg.JUDGE_PROVIDER)
    except llm.ConfigError as e:
        sys.exit(str(e))
    if hasattr(client, "temperature"):
        client.temperature = cfg.JUDGE_TEMPERATURE
    tasks = load(PLUS_PATH)
    settings = {"judge_provider": cfg.JUDGE_PROVIDER, "judge_model": cfg.JUDGE_MODEL,
                "temperature": cfg.JUDGE_TEMPERATURE, "rubric_file": RUBRIC_PATH.name,
                "rubric_sha256": RUBRIC_SHA, "rubric": RUBRIC_TEXT,
                "step1_prompt": STEP1_PROMPT, "step2_prompt": STEP2_PROMPT}

    jobs = []
    for run in args.runs:
        run = run.resolve()
        out_dir = run / "disclosure"
        out_dir.mkdir(exist_ok=True)
        (out_dir / "prompts.json").write_text(json.dumps(settings, indent=2))
        for f in sorted((run / "dialogues").glob("*.json")):
            if args.match and not any(m in f.stem for m in args.match):
                continue
            d = json.loads(f.read_text())
            if d.get("error"):
                print(f"skipped {run.name}/{f.stem}: dialogue ended in an error")
                continue
            if (out_dir / f.name).exists() and not args.force:
                continue
            jobs.append((run, f, d))
    print(f"Judging {len(jobs)} dialogues with {cfg.JUDGE_MODEL}")

    def work(job):
        run, f, d = job
        turns = judge_dialogue(client, d, tasks[d["task_id"]]["prompt"].strip())
        out = {"dialogue": f"{run.name}/{f.stem}", "condition": d["condition"], "task_id": d["task_id"],
               "repeat": d["repeat"], "judge_model": cfg.JUDGE_MODEL, "rubric_sha256": RUBRIC_SHA,
               "turns": turns}
        (run / "disclosure" / f.name).write_text(json.dumps(out, indent=2))
        labels = " ".join({"disclosure": "D", "no_disclosure": ".", "unclear": "?",
                           "not_applicable": "-"}.get(t.get("label"), "E") + ("+" if t.get("new") else " ")
                          for t in turns)
        print(f"  {d['condition']:<15}{d['task_id']:<14}{labels}", flush=True)
        return out

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, jobs))

    results = []
    for run in {j[0] for j in jobs} | {r.resolve() for r in args.runs}:
        write_csv(run)
        results += [json.loads(f.read_text()) for f in sorted((run / "disclosure").glob("*__*.json"))
                    if not args.match or any(m in f.stem for m in args.match)]
    print("\nLabels per turn: D disclosure (+ new), . no disclosure, ? unclear, - not applicable, E error")
    summarize(results)


if __name__ == "__main__":
    main()
