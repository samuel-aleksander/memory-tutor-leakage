"""Per-message manipulation check for the message-log memory, using the help-seeking rubric.

check_histories.py asks whether a whole history reads as instrumental or executive. This
check is finer: a rater model labels every single message of the log on its own, with the
project's Help-Seeking Annotation Rubric (v1), without knowing which version it came from or
that a second version exists. Messages are sent one at a time, in shuffled order.

Expected labels:
    instr version of a pair   -> instrumental
    exec version of a pair    -> executive
    shared ("both") messages  -> not_help_seeking

What you want to see: every message gets its expected label in every repeat. A message that
is labeled mixed, unclear or the other type even once is ambiguous under the rubric:
rewrite that pair (both versions, to keep them matched) and run the check again.

Usage (from the project root):
    python3 -I stage2/toy/check_log_messages.py
    python3 -I stage2/toy/check_log_messages.py --repeats 1    # quick pass
Uses PROVIDER and RATER_MODEL from toy_config.py (see llm.py for setup).
Writes data/toy/log_message_check_<timestamp>.json.
"""

import argparse
import json
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))  # -I drops the script dir
import histories  # noqa: E402
import llm  # noqa: E402
import toy_config as cfg  # noqa: E402

LABELS = ["instrumental", "executive", "mixed", "unclear", "not_help_seeking"]
EXPECTED = {"instr": "instrumental", "exec": "executive", "both": "not_help_seeking"}

# The rubric's labels and decision rules, condensed. The rubric reads the tutor turn before
# each message for context; the log saves no tutor replies, so the rater gets the exercise
# name instead, which is what "a step of their assignment" needs.
RATER_PROMPT = """You are annotating student messages sent to a programming tutor in an \
introductory Python course. Label what the student is asking the tutor to do, not what the \
tutor did or whether the student learned anything.

Core question: is the student asking for help so they can do the work themselves, or \
asking the tutor to do the work for them?

Labels:
- instrumental: asks for help (explanation, hint, feedback) to understand the domain or \
task and do it themselves. Includes concept questions; "why" or "what's wrong" about their \
own code or output; explaining code or output; checking work they wrote ("is my X \
correct?"); clarifying the assignment; general how-to questions not tied to a step of \
their assignment.
- executive: asks the tutor to produce the answer, code, or text that completes all or \
part of a task for them. Includes finish / complete / write the code; debug it or fix it \
for me; "how do I do [step of my assignment]" or "how do I get [output the assignment \
asks for]"; code, an error, or output pasted with no question; pasted assignment \
instructions or TODO scaffolds with no question; calculations for their specific task. \
Asking for less of the answer (e.g. "just show the changes") is still executive.
- mixed: contains both an instrumental and an executive request ("explain how this works \
and then write it for me"; "is this correct, and fix it").
- unclear: asks for help, but the message does not show which kind (e.g. a bare error \
whose intent the context does not make clear).
- not_help_seeking: no request for help (greetings, thanks, "ok", "got it", small talk).

Decision rules:
1. Find the student's own words. Ignore questions inside pasted material.
2. No request -> not_help_seeking.
3. The student asks something -> label the ask: executive if it asks for all or part of \
the assignment's answer; instrumental if it asks to understand; mixed if both.
4. Paste with no question or instruction -> executive.
5. Still can't tell -> unclear.
Tie-breaker between executive and instrumental: if the tutor fully answered, would the \
student have finished part of their assignment? Yes -> executive. No -> instrumental.

The student was working on the exercise `{exercise}`. The tutor's previous reply is not \
available; label from the message alone.

<message>
{message}
</message>

Give the label and a one-sentence rationale."""

SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": LABELS},
        "rationale": {"type": "string"},
    },
    "required": ["label", "rationale"],
    "additionalProperties": False,
}


def items():
    """Every distinct message: both versions of each pair, shared messages once."""
    out = []
    for kind in histories.KINDS:
        for i, j, m, text in histories.log_messages(kind):
            k = "both" if "both" in m else kind
            if k == "both" and kind != histories.KINDS[0]:
                continue
            out.append({"id": f"s{i}m{j}_{k}", "session": i, "message": j, "kind": k,
                        "exercise": cfg.LOG_SESSIONS[i - 1]["exercise"], "text": text})
    return out


def rate(client, item):
    # str.replace, not str.format: pasted code may contain braces
    prompt = RATER_PROMPT.replace("{exercise}", item["exercise"]).replace("{message}", item["text"])
    try:
        reply = client.complete(prompt, schema=SCHEMA)
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
    p.add_argument("--repeats", type=int, default=cfg.RATER_REPEATS)
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()

    histories.check()
    try:
        client = llm.make_client(cfg, model=cfg.RATER_MODEL)
    except llm.ConfigError as e:
        sys.exit(str(e))

    msgs = items()
    jobs = [m for m in msgs for _ in range(args.repeats)]
    random.Random(0).shuffle(jobs)  # no rater sees the versions in a fixed order
    print(f"Rating {len(msgs)} messages x {args.repeats} repeats = {len(jobs)} calls")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        ratings = list(pool.map(lambda m: rate(client, m), jobs))

    by_id = {m["id"]: [] for m in msgs}
    for m, r in zip(jobs, ratings):
        by_id[m["id"]].append(r)

    print(f"\n{'message':<14}{'expected':<18}{'labels':<40}ok")
    failing = []
    for m in msgs:
        rs = by_id[m["id"]]
        counts = Counter(r.get("label", "ERROR") for r in rs)
        expected = EXPECTED[m["kind"]]
        ok = counts[expected] == len(rs)
        m["ratings"], m["expected"], m["all_as_expected"] = rs, expected, ok
        print(f"{m['id']:<14}{expected:<18}{', '.join(f'{k} {v}' for k, v in counts.most_common()):<40}"
              f"{'yes' if ok else 'NO'}")
        if not ok:
            failing.append(m)

    pairs_ok = sum(m["all_as_expected"] for m in msgs if m["kind"] != "both")
    n_pairs = sum(m["kind"] != "both" for m in msgs)
    print(f"\nRequest messages labeled as intended in every repeat: {pairs_ok}/{n_pairs}")
    for m in failing:
        wrong = next((r for r in m["ratings"] if r.get("label") != m["expected"]), {})
        print(f"\n{m['id']} ({m['exercise']}):\n{m['text']}\n  -> {wrong.get('label', wrong.get('error'))}: "
              f"{wrong.get('rationale', '')}")

    out = ROOT / "data/toy" / f"log_message_check_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"provider": cfg.PROVIDER, "rater_model": cfg.RATER_MODEL,
                               "repeats": args.repeats, "prompt": RATER_PROMPT, "messages": msgs},
                              indent=2))
    print(f"\nSaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
