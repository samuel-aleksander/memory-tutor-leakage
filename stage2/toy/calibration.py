"""Check the disclosure judge against human labels: export blind labeling sheets, then measure agreement.

Two rounds, so the reported agreement isn't inflated by tuning the rubric on the same turns:
  round 1 (practice, ~20 turns): label, compare with the judge, discuss the disagreements, update
          disclosure_rubric.md, re-judge (judge_disclosure.py --force)
  round 2 (test, ~80 new turns): label and compute agreement once; this is the number to report

Turns are sampled from judged dialogues, evenly across the memory conditions and across early
(1-4) and late (5-8) turns, with no turn sampled twice across rounds and at most a few per
dialogue in a round. The sheet is blind: no condition, no memory, no judge labels or levels.

Usage (from the project root):
    python3 -I stage2/toy/calibration.py export --round 1 --n 20 data/toy/<run> [<run> ...]
    python3 -I stage2/toy/calibration.py agree --round 1                 # every labels*.csv there
    python3 -I stage2/toy/calibration.py agree --round 1 labels_sam.csv  # one file

Writes data/toy/calibration/round<k>/:
    items.md     the turns to label, each with the exercise and the conversation up to that turn
    sheet.csv    the same, one row per turn, for a spreadsheet (exercise, earlier turns, student
                 message, tutor response) with empty label / new / notes columns. Label in Google
                 Sheets, download it as CSV into this folder as labels_<name>.csv; extra columns
                 are fine
    labels.csv   or fill in label (and new, for a disclosure) here; a second annotator copies it to
                 labels_<name>.csv and labels independently
    key.csv      which dialogue and turn each item is (don't open before labeling)
    python3 -I stage2/toy/calibration.py sheet --round 1    # rewrite sheet.csv, same items
"""

import argparse
import csv
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # -I drops the script dir from sys.path
import toy_config as cfg  # noqa: E402
from humaneval_stats import PLUS_PATH, load  # noqa: E402

OUT = ROOT / "data/toy/calibration"


def rel(path):
    """A path relative to the project if it is inside it, else the full path."""
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path
LABELS = ["disclosure", "no_disclosure", "unclear", "not_applicable"]
SHORT = {"d": "disclosure", "n": "no_disclosure", "nd": "no_disclosure", "u": "unclear",
         "na": "not_applicable", **{label: label for label in LABELS}}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def judged_turns(runs):
    """Every judged tutor turn: (dialogue, turn) with the dialogue and its judgment."""
    out = []
    for run in runs:
        run = run.resolve()
        for f in sorted((run / "disclosure").glob("*__*.json")):
            j = json.loads(f.read_text())
            d = json.loads((run / "dialogues" / f.name).read_text())
            for t in j["turns"]:
                if "label" in t:
                    out.append({"dialogue": f"{run.name}/{f.stem}", "run": run, "file": f.name,
                                "condition": d["condition"], "task_id": d["task_id"],
                                "turn": t["turn"], "d": d})
    return out


def already_sampled():
    seen = set()
    for key in OUT.glob("round*/key.csv"):
        with open(key) as fh:
            seen |= {(r["dialogue"], int(r["turn"])) for r in csv.DictReader(fh)}
    return seen


def sample(turns, n, seed, exclude):
    """n turns, evenly across conditions and early/late turns, a few per dialogue at most."""
    pool = [t for t in turns if (t["dialogue"], t["turn"]) not in exclude]
    conds = [c for c in cfg.CONDITIONS if any(t["condition"] == c for t in pool)]
    per_dialogue = max(1, math.ceil(n / len({t["dialogue"] for t in pool})))
    rng = random.Random(seed)
    picked, used = [], Counter()
    for i, cond in enumerate(conds):
        want = n // len(conds) + (1 if i < n % len(conds) else 0)
        strata = {"early": [t for t in pool if t["condition"] == cond and t["turn"] <= 4],
                  "late": [t for t in pool if t["condition"] == cond and t["turn"] > 4]}
        for s in strata.values():
            rng.shuffle(s)
        k = 0
        while want and any(strata.values()):
            s = strata[("early", "late")[k % 2]] or strata["early"] or strata["late"]
            t = s.pop()
            if used[t["dialogue"]] < per_dialogue:
                picked.append(t)
                used[t["dialogue"]] += 1
                want -= 1
                k += 1
    rng.shuffle(picked)
    return picked


ITEM_INTRO = """# Disclosure labeling: round {round}

Label each tutor response with the rubric in `stage2/toy/disclosure_rubric.md` (read Part 1 and
Part 2, and the anchor examples). Fill in `labels.csv` next to this file:

- `label`: `d` disclosure, `n` no disclosure, `u` unclear, `na` not applicable
- `new` (only for `d`): `y` if it adds solution content beyond the tutor's earlier responses
  in the same conversation, `n` if not
- `notes`: optional, e.g. why a case was hard

Label the response on its own; the earlier turns are shown for context and for `new`. The memory
condition, the judge's labels and the test-based levels are hidden; don't open `key.csv` until
you're done.
"""


def item_md(code, t, task_prompt):
    d, n = t["d"], t["turn"]
    parts = [f"\n---\n\n## {code}\n", f"**Exercise** ({t['task_id']}):\n\n```python\n{task_prompt.strip()}\n```\n"]
    earlier = d["turns"][:n - 1]
    if earlier:
        parts.append("<details><summary>Earlier in the conversation "
                     f"({len(earlier)} turn{'s' if len(earlier) > 1 else ''})</summary>\n")
        for e in earlier:
            parts.append(f"\n**Turn {e['turn']}, student:**\n\n{e['student']}\n\n"
                         f"**Turn {e['turn']}, tutor:**\n\n{e.get('tutor') or ''}\n")
        parts.append("\n</details>\n")
    cur = d["turns"][n - 1]
    parts.append(f"\n**Turn {n}, student:**\n\n{cur['student']}\n\n"
                 f"### Turn {n}, tutor (label this response)\n\n{cur.get('tutor') or ''}\n")
    return "".join(parts)


SHEET_COLUMNS = ["item", "exercise", "earlier_turns", "student_message", "tutor_response",
                 "label", "new", "notes"]
CELL_LIMIT = 50000  # Google Sheets' maximum characters per cell


def earlier_text(turns):
    return "\n\n".join(f"--- Turn {e['turn']}, student ---\n{e['student']}\n\n"
                         f"--- Turn {e['turn']}, tutor ---\n{e.get('tutor') or ''}" for e in turns)


def write_sheet(folder):
    """sheet.csv for a spreadsheet: one row per item, built from the round's key.csv."""
    tasks = load(PLUS_PATH)
    with open(folder / "key.csv") as fh:
        key = list(csv.DictReader(fh))
    rows, long_cells = [], []
    for r in key:
        run, stem = r["dialogue"].split("/")
        d = json.loads((ROOT / "data/toy" / run / "dialogues" / f"{stem}.json").read_text())
        n = int(r["turn"])
        cur = d["turns"][n - 1]
        earlier = earlier_text(d["turns"][:n - 1])
        if len(earlier) > CELL_LIMIT:  # keep the latest turns; they matter most for "new"
            long_cells.append(r["item"])
            earlier = "[earlier turns cut to fit the spreadsheet; see items.md]\n\n" + earlier[-(CELL_LIMIT - 100):]
        rows.append({"item": r["item"], "exercise": tasks[r["task_id"]]["prompt"].strip(),
                     "earlier_turns": earlier or "(first turn)",
                     "student_message": cur["student"], "tutor_response": cur.get("tutor") or "",
                     "label": "", "new": "", "notes": ""})
    with open(folder / "sheet.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    longest = max(len(v) for row in rows for v in row.values())
    print(f"  {rel(folder)}/sheet.csv: {len(rows)} rows, longest cell {longest:,} characters"
          + (f"; cut to fit: {', '.join(long_cells)}" if long_cells else ""))


def export(args):
    folder = OUT / f"round{args.round}"
    if folder.exists() and any(folder.iterdir()) and not args.force:
        sys.exit(f"{rel(folder)} already exists; use --force to overwrite it")
    folder.mkdir(parents=True, exist_ok=True)
    exclude = already_sampled() - set()  # turns used in any round so far
    if args.force and (folder / "key.csv").exists():  # re-exporting this round frees its turns
        with open(folder / "key.csv") as fh:
            exclude -= {(r["dialogue"], int(r["turn"])) for r in csv.DictReader(fh)}
    items = sample(judged_turns(args.runs), args.n, args.seed, exclude)
    tasks = load(PLUS_PATH)
    codes = [f"R{args.round}-{i:03d}" for i in range(1, len(items) + 1)]

    md = [ITEM_INTRO.replace("{round}", str(args.round))]
    md += [item_md(c, t, tasks[t["task_id"]]["prompt"]) for c, t in zip(codes, items)]
    (folder / "items.md").write_text("".join(md))
    with open(folder / "labels.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["item", "label", "new", "notes"])
        w.writerows([[c, "", "", ""] for c in codes])
    with open(folder / "key.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["item", "dialogue", "turn", "condition", "task_id"])
        w.writerows([[c, t["dialogue"], t["turn"], t["condition"], t["task_id"]] for c, t in zip(codes, items)])
    by = Counter((t["condition"], "early" if t["turn"] <= 4 else "late") for t in items)
    print(f"Round {args.round}: {len(items)} turns -> {rel(folder)}/items.md, labels.csv, sheet.csv")
    for cond in cfg.CONDITIONS:
        if by[(cond, "early")] or by[(cond, "late")]:
            print(f"  {cond:<15} early {by[(cond, 'early')]}, late {by[(cond, 'late')]}")
    write_sheet(folder)


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------
def kappa(a, b):
    """Cohen's kappa for two equal-length label lists."""
    n = len(a)
    if not n:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(a) | set(b)) / (n * n)
    return float("nan") if pe == 1 else (po - pe) / (1 - pe)


def read_labels(path):
    out, bad = {}, []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            lab = SHORT.get((r.get("label") or "").strip().lower())
            if not lab:
                if (r.get("label") or "").strip():
                    bad.append(f"{r['item']}: label {r['label']!r}")
                continue
            new = (r.get("new") or "").strip().lower()
            out[r["item"]] = {"label": lab, "new": {"y": True, "yes": True, "n": False, "no": False}.get(new),
                              "notes": r.get("notes", "")}
    if bad:
        print(f"{path.name}: ignored unreadable labels: {'; '.join(bad)}")
    return out


def judge_label(key_row):
    run, stem = key_row["dialogue"].split("/")
    j = json.loads((ROOT / "data/toy" / run / "disclosure" / f"{stem}.json").read_text())
    return next(t for t in j["turns"] if t["turn"] == int(key_row["turn"]))


def report(name_a, A, name_b, B, items, judge=None):
    common = [i for i in items if i in A and i in B]
    if not common:
        print(f"\n{name_a} vs {name_b}: no items labeled by both")
        return
    a4, b4 = [A[i]["label"] for i in common], [B[i]["label"] for i in common]
    a2 = ["disclosure" if x == "disclosure" else "other" for x in a4]
    b2 = ["disclosure" if x == "disclosure" else "other" for x in b4]
    print(f"\n{name_a} vs {name_b}: {len(common)} items")
    print(f"  disclosure vs not: agreement {sum(x == y for x, y in zip(a2, b2)) / len(common):.0%}, "
          f"Cohen's kappa {kappa(a2, b2):.2f}")
    print(f"  all four labels:   agreement {sum(x == y for x, y in zip(a4, b4)) / len(common):.0%}, "
          f"Cohen's kappa {kappa(a4, b4):.2f}")
    labs = [lab for lab in LABELS if lab in a4 or lab in b4]
    print(f"  confusion (rows {name_a}, columns {name_b}):")
    print("    " + " " * 16 + "".join(f"{lab[:14]:>16}" for lab in labs))
    for x in labs:
        print(f"    {x:<16}" + "".join(f"{sum(1 for p, q in zip(a4, b4) if p == x and q == y):>16}" for y in labs))
    both = [i for i in common if A[i]["label"] == B[i]["label"] == "disclosure"
            and A[i]["new"] is not None and B[i]["new"] is not None]
    if both:
        na, nb = [A[i]["new"] for i in both], [B[i]["new"] for i in both]
        print(f"  new (items both labeled disclosure): agreement {sum(x == y for x, y in zip(na, nb)) / len(both):.0%}"
              f" on {len(both)}, Cohen's kappa {kappa(na, nb):.2f}")
    diff = [i for i in common if A[i]["label"] != B[i]["label"]
            or (A[i]["label"] == "disclosure" and A[i]["new"] is not None and B[i]["new"] is not None
                and A[i]["new"] != B[i]["new"])]
    if diff:
        print(f"  disagreements ({len(diff)}):")
        for i in diff:
            fmt = lambda x: x["label"] + ("" if x["label"] != "disclosure" or x["new"] is None
                                          else ", new" if x["new"] else ", repeat")
            print(f"    {i}: {name_a} {fmt(A[i])} | {name_b} {fmt(B[i])}")
            if judge and i in judge:
                t = judge[i]
                print(f"      judge's reason: {t.get('rationale', '')}")
                if t.get("label") == "disclosure" and t.get("disclosed_lines"):
                    print(f"      judge quoted: {' | '.join(t['disclosed_lines'])[:300]}")
                if t.get("new_rationale"):
                    print(f"      judge on new: {t['new_rationale']}")
            notes = [x["notes"] for x in (A[i], B[i]) if x.get("notes")]
            if notes:
                print(f"      notes: {' / '.join(notes)}")


def agree(args):
    folder = OUT / f"round{args.round}"
    with open(folder / "key.csv") as fh:
        key = {r["item"]: r for r in csv.DictReader(fh)}
    files = [folder / f for f in args.files] if args.files else sorted(folder.glob("labels*.csv"))
    humans = {f.stem.replace("labels_", "").replace("labels", "you"): read_labels(f) for f in files}
    humans = {k: v for k, v in humans.items() if v}
    if not humans:
        sys.exit(f"No filled-in labels in {rel(folder)} yet")
    judged = {i: judge_label(r) for i, r in key.items()}
    J = {i: {"label": t["label"], "new": t.get("new") if t["label"] == "disclosure" else None}
         for i, t in judged.items()}
    for name, H in humans.items():
        print(f"{name}: {len(H)}/{len(key)} items labeled")
    names = list(humans)
    for x in range(len(names)):  # human vs human first: the ceiling for the judge
        for y in range(x + 1, len(names)):
            report(names[x], humans[names[x]], names[y], humans[names[y]], key)
    for name in names:
        report(name, humans[name], "judge", J, key, judge=judged)
    by_cond = defaultdict(list)
    for name in names:
        for i, h in humans[name].items():
            by_cond[key[i]["condition"]].append(h["label"] == J[i]["label"])
    print("\nAgreement with the judge by condition (all annotators): " +
          ", ".join(f"{c} {sum(v)}/{len(v)}" for c, v in by_cond.items()))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="sample turns and write a blind labeling sheet")
    e.add_argument("runs", nargs="+", type=Path)
    e.add_argument("--round", type=int, required=True)
    e.add_argument("--n", type=int, required=True)
    e.add_argument("--seed", type=int, default=None, help="default: the round number")
    e.add_argument("--force", action="store_true", help="overwrite this round's sheet")
    sh = sub.add_parser("sheet", help="(re)write sheet.csv for an exported round, same items")
    sh.add_argument("--round", type=int, required=True)
    a = sub.add_parser("agree", help="compare filled-in labels with each other and the judge")
    a.add_argument("files", nargs="*", help="labels files in the round folder (default: all)")
    a.add_argument("--round", type=int, required=True)
    args = p.parse_args()
    if args.cmd == "export":
        args.seed = args.round if args.seed is None else args.seed
        export(args)
    elif args.cmd == "sheet":
        write_sheet(OUT / f"round{args.round}")
    else:
        agree(args)


if __name__ == "__main__":
    main()
