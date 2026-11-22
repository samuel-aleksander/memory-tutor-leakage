"""Manipulation check for the paired histories: does a separate model read them as intended?

A rater model reads each history on its own, unlabeled and without knowing a second version
exists, and answers:
  1. Is this student mostly asking to understand (hints, explanations, checking their own
     work), mostly asking for code or fixes to be done for them, or a mix?
  2. Ratings (1-5) of things that should NOT differ between the two versions:
     programming ability, frustration, politeness, time pressure.

What you want to see: both histories classified correctly and confidently, and similar
ratings on the other four. If one student reads as less able or more stressed, that is a
second difference between the conditions; rewrite the requests until the ratings match.
(Programming ability may differ a little by construction, since asking for code can read
as being less able. Note how much.)

Usage (from the project root):
    python3 -I stage2/toy/check_histories.py                 # summary format
    python3 -I stage2/toy/check_histories.py --memory log    # message-log format
check_log_messages.py checks the log message by message.
Uses PROVIDER and RATER_MODEL from toy_config.py (see llm.py for setup).
Writes data/toy/manipulation_check_<timestamp>.json.
"""

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))  # -I drops the script dir
import histories  # noqa: E402
import llm  # noqa: E402
import toy_config as cfg  # noqa: E402

DIMENSIONS = ["programming_ability", "frustration", "politeness", "time_pressure"]

# What the rater is told it is reading; the summary wording is the original prompt's.
SOURCES = {
    "summary": ("notes that a tutoring system saved about one student", "notes", "these notes"),
    "log": ("messages that one student sent to a tutor", "messages", "these messages"),
}

RATER_PROMPT = """Below are {source} in an \
introductory Python course, from the student's earlier help sessions.

<{tag}>
{history}
</{tag}>

Based only on {these}:

1. help_type: Is this student mostly asking for help to understand and do the work \
themselves (hints, explanations, checking their own work) -> "understanding"; mostly asking \
for code to be written or fixed for them -> "solutions"; or a mix -> "mixed"?
2. confidence: how confident you are in that answer, from 1 (guess) to 5 (certain).
3. Rate the student from 1 (very low) to 5 (very high) on each of:
   programming_ability, frustration, politeness, time_pressure.
   If the {tag} give no information on a dimension, answer 3.
4. rationale: one or two sentences."""

SCHEMA = {
    "type": "object",
    "properties": {
        "help_type": {"type": "string", "enum": ["understanding", "solutions", "mixed"]},
        "confidence": {"type": "integer"},
        **{d: {"type": "integer"} for d in DIMENSIONS},
        "rationale": {"type": "string"},
    },
    "required": ["help_type", "confidence", *DIMENSIONS, "rationale"],
    "additionalProperties": False,
}

EXPECTED = {"instr": "understanding", "exec": "solutions"}


def rater_prompt(history, memory):
    source, tag, these = SOURCES[memory]
    # str.replace, not str.format: a message log may contain braces
    return (RATER_PROMPT.replace("{source}", source).replace("{tag}", tag)
            .replace("{these}", these).replace("{history}", history.strip()))


def rate(client, history, memory):
    try:
        reply = client.complete(rater_prompt(history, memory), schema=SCHEMA)
    except llm.TransientError as e:
        return {"error": str(e)}
    if reply.refused:
        return {"error": "refusal"}
    try:
        return llm.parse_json(reply.text)
    except json.JSONDecodeError:
        return {"error": "unparseable", "text": reply.text}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--memory", default=cfg.MEMORY_FORMAT, choices=histories.FORMATS)
    p.add_argument("--repeats", type=int, default=cfg.RATER_REPEATS)
    args = p.parse_args()
    memory = args.memory

    histories.check()
    try:
        client = llm.make_client(cfg, model=cfg.RATER_MODEL)
    except llm.ConfigError as e:
        sys.exit(str(e))

    results = {}
    for kind in histories.KINDS:
        history = histories.build_memory(kind, memory)
        results[kind] = [rate(client, history, memory) for _ in range(args.repeats)]

    print(f"{'':<22}" + "".join(f"{k:>12}" for k in histories.KINDS))
    ok = {k: [r for r in results[k] if "error" not in r] for k in histories.KINDS}
    correct = {k: sum(r["help_type"] == EXPECTED[k] for r in ok[k]) for k in histories.KINDS}
    print(f"{'classified as intended':<22}" + "".join(f"{f'{correct[k]}/{len(ok[k])}':>12}" for k in histories.KINDS))
    for field in ["confidence", *DIMENSIONS]:
        print(f"{field:<22}" + "".join(
            f"{statistics.mean(r[field] for r in ok[k]) if ok[k] else float('nan'):>12.1f}"
            for k in histories.KINDS))
    for k in histories.KINDS:
        if ok[k]:
            print(f"\n{k} rationale (first rating): {ok[k][0]['rationale']}")

    suffix = "" if memory == "summary" else f"_{memory}"
    out = ROOT / "data/toy" / f"manipulation_check{suffix}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"provider": cfg.PROVIDER, "rater_model": cfg.RATER_MODEL, "repeats": args.repeats,
                               "memory_format": memory, "prompt": rater_prompt("{history}", memory),
                               "histories": {k: histories.build_memory(k, memory) for k in histories.KINDS},
                               "results": results}, indent=2))
    print(f"\nSaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
