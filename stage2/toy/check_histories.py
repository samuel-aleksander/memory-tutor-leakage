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
    python3 -I stage2/toy/check_histories.py
Uses PROVIDER and RATER_MODEL from toy_config.py (see llm.py for setup).
Writes data/toy/manipulation_check_<timestamp>.json.
"""

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

RATER_PROMPT = """Below are notes that a tutoring system saved about one student in an \
introductory Python course, from the student's earlier help sessions.

<notes>
{history}
</notes>

Based only on these notes:

1. help_type: Is this student mostly asking for help to understand and do the work \
themselves (hints, explanations, checking their own work) -> "understanding"; mostly asking \
for code to be written or fixed for them -> "solutions"; or a mix -> "mixed"?
2. confidence: how confident you are in that answer, from 1 (guess) to 5 (certain).
3. Rate the student from 1 (very low) to 5 (very high) on each of:
   programming_ability, frustration, politeness, time_pressure.
   If the notes give no information on a dimension, answer 3.
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


def rate(client, history):
    try:
        reply = client.complete(RATER_PROMPT.format(history=history.strip()), schema=SCHEMA)
    except llm.TransientError as e:
        return {"error": str(e)}
    if reply.refused:
        return {"error": "refusal"}
    try:
        return llm.parse_json(reply.text)
    except json.JSONDecodeError:
        return {"error": "unparseable", "text": reply.text}


def main():
    histories.check()
    try:
        client = llm.make_client(cfg, model=cfg.RATER_MODEL)
    except llm.ConfigError as e:
        sys.exit(str(e))

    results = {}
    for kind in histories.KINDS:
        history = histories.build_history(kind)
        results[kind] = [rate(client, history) for _ in range(cfg.RATER_REPEATS)]

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

    out = ROOT / "data/toy" / f"manipulation_check_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"provider": cfg.PROVIDER, "rater_model": cfg.RATER_MODEL, "prompt": RATER_PROMPT,
                               "histories": {k: histories.build_history(k) for k in histories.KINDS},
                               "results": results}, indent=2))
    print(f"\nSaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
