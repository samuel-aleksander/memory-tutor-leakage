"""Build the paired memory histories from toy_config.py and check they are matched.

Two memory formats, both built from one template per format, so structure (sessions, weeks,
exercises, order, number of requests, header, pasted material) is identical by construction:
    summary  third-person notes, one line per request (toy_config.SESSIONS)
    log      the student's own past messages (toy_config.LOG_SESSIONS)
This module checks what the templates can't guarantee: that each request pair, and the
histories as a whole, have about the same length.

The log format also has a neutral history (condition D_neutral, toy_config.NEUTRAL_LOG_SESSIONS):
the same sessions and number of messages, about the same length, but no help-seeking. It is
built in the same layout and checked for the same structure and length.

Usage (from the project root):
    python3 -I stage2/toy/histories.py                 # summary format
    python3 -I stage2/toy/histories.py --memory log    # message-log format, plus the neutral log
Prints the histories, the length table and a diff.
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir
import toy_config as cfg  # noqa: E402

KINDS = ("instr", "exec")  # the paired histories
NEUTRAL = "neutral"        # the neutral log (log format only)
FORMATS = ("summary", "log")
PASTE_KEYS = (("exercise", ""), ("announcement", ""), ("code", "python"), ("output", ""),
              ("error", ""))  # shown in this order
POLITENESS = ("please", "thanks", "thank you", "sorry")  # must appear in both versions or neither


def sentence_form(text):
    """"question" if the student's words end in a question mark, else "command"."""
    return "question" if text.rstrip().endswith("?") else "command"


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def build_history(kind):
    """Summary memory block for one kind of history ("instr" or "exec")."""
    lines = [cfg.HISTORY_HEADER]
    for i, s in enumerate(cfg.SESSIONS, 1):
        lines.append(f"\nSession {i} (week {s['week']}), exercise `{s['exercise']}`:")
        lines += [f"- {r[kind]}" for r in s["requests"]]
    return "\n".join(lines)


def message_text(m, kind):
    """One student message of the log: pasted material (shared), then the student's words."""
    parts = [f"```{lang}\n{m[key]}\n```" for key, lang in PASTE_KEYS if key in m]
    parts.append(m["both"] if "both" in m else m[kind])
    return "\n".join(parts)


def log_sessions(kind):
    """(week, exercise, messages) per session: the neutral log reuses LOG_SESSIONS' weeks and
    exercises with its own messages."""
    if kind == NEUTRAL:
        return [(s["week"], s["exercise"], msgs)
                for s, msgs in zip(cfg.LOG_SESSIONS, cfg.NEUTRAL_LOG_SESSIONS)]
    return [(s["week"], s["exercise"], s["messages"]) for s in cfg.LOG_SESSIONS]


def log_messages(kind):
    """(session number, message number, message dict, text) for every message in the log."""
    return [(i, j, m, message_text(m, kind))
            for i, (_, _, msgs) in enumerate(log_sessions(kind), 1)
            for j, m in enumerate(msgs, 1)]


def build_log(kind):
    """Message-log memory block for one kind of history ("instr", "exec" or "neutral")."""
    lines = [cfg.LOG_HEADER]
    for i, (week, exercise, msgs) in enumerate(log_sessions(kind), 1):
        lines.append(f"\n### Session {i} (week {week}), exercise `{exercise}`")
        for m in msgs:
            text = message_text(m, kind)
            # a code fence must start its own line to render
            lines.append(f"\nStudent:\n{text}" if text.startswith("```") else f"\nStudent: {text}")
    return "\n".join(lines)


def available_kinds(memory):
    """The histories a format has: the neutral one exists only as a log."""
    return KINDS + ((NEUTRAL,) if memory == "log" else ())


def build_memory(kind, memory):
    """Memory block in the given format ("summary" or "log")."""
    if kind not in available_kinds(memory):
        raise ValueError(f"no {kind} history in the {memory} format")
    return {"summary": build_history, "log": build_log}[memory](kind)


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------
def request_pairs(memory):
    """(label, pair dict) for every request that differs between the two histories."""
    if memory == "summary":
        return [(f"session {i} request {j}", r)
                for i, s in enumerate(cfg.SESSIONS, 1) for j, r in enumerate(s["requests"], 1)]
    return [(f"session {i} message {j}", m) for i, j, m, _ in log_messages(KINDS[0])
            if "both" not in m]


def shape_problems():
    """Log messages that are neither a pair nor a shared message, or mix the two."""
    problems = []
    for i, s in enumerate(cfg.LOG_SESSIONS, 1):
        for j, m in enumerate(s["messages"], 1):
            keys = set(m) - {k for k, _ in PASTE_KEYS}
            if keys == {"both"}:
                if any(k in m for k, _ in PASTE_KEYS):
                    problems.append(f"log session {i} message {j}: 'both' messages take no paste")
            elif keys != set(KINDS) or not all(m[k].strip() for k in KINDS):
                problems.append(f"log session {i} message {j}: needs non-empty 'instr' and "
                                f"'exec', or only 'both' (has {sorted(keys)})")
    return problems


def length_problems(memory):
    """List every request pair or history that breaks the length tolerances."""
    problems = []
    for label, r in request_pairs(memory):
        if set(KINDS) - set(r) or not all(r[k].strip() for k in KINDS):
            problems.append(f"{memory} {label}: needs non-empty 'instr' and 'exec'")
            continue
        diff = abs(len(r["instr"].split()) - len(r["exec"].split()))
        if diff > cfg.MAX_PAIR_WORD_DIFF:
            problems.append(f"{memory} {label}: {diff} words apart (max {cfg.MAX_PAIR_WORD_DIFF})")
    a, b = (len(build_memory(k, memory)) for k in KINDS)
    rel = abs(a - b) / max(a, b)
    if rel > cfg.MAX_TOTAL_LENGTH_DIFF:
        problems.append(f"{memory} histories differ in length by {rel:.1%} "
                        f"(max {cfg.MAX_TOTAL_LENGTH_DIFF:.0%})")
    return problems


def form_problems():
    """Log pairs whose two versions differ in sentence form or politeness words."""
    problems = []
    for label, r in request_pairs("log"):
        forms = {k: sentence_form(r[k]) for k in KINDS}
        if len(set(forms.values())) > 1:
            problems.append(f"log {label}: sentence forms differ ({forms['instr']} vs {forms['exec']})")
        for word in POLITENESS:
            has = {k: bool(re.search(rf"\b{word}\b", r[k].lower())) for k in KINDS}
            if len(set(has.values())) > 1:
                problems.append(f"log {label}: '{word}' in only one version")
    return problems


def neutral_problems():
    """The neutral log must match the paired logs in structure and (roughly) length."""
    problems = []
    if len(cfg.NEUTRAL_LOG_SESSIONS) != len(cfg.LOG_SESSIONS):
        return [f"neutral log has {len(cfg.NEUTRAL_LOG_SESSIONS)} sessions, "
                f"LOG_SESSIONS has {len(cfg.LOG_SESSIONS)}"]
    for i, (s, msgs) in enumerate(zip(cfg.LOG_SESSIONS, cfg.NEUTRAL_LOG_SESSIONS), 1):
        if len(msgs) != len(s["messages"]):
            problems.append(f"neutral session {i}: {len(msgs)} messages, LOG_SESSIONS has "
                            f"{len(s['messages'])}")
        for j, m in enumerate(msgs, 1):
            if set(m) - {k for k, _ in PASTE_KEYS} != {NEUTRAL} or not m[NEUTRAL].strip():
                problems.append(f"neutral session {i} message {j}: needs a non-empty 'neutral' "
                                f"(plus optional pasted material) and nothing else")
    if problems:
        return problems
    a, b = len(build_log(NEUTRAL)), max(len(build_log(k)) for k in KINDS)
    rel = abs(a - b) / max(a, b)
    if rel > cfg.MAX_TOTAL_LENGTH_DIFF:
        problems.append(f"neutral log differs in length from the paired logs by {rel:.1%} "
                        f"(max {cfg.MAX_TOTAL_LENGTH_DIFF:.0%})")
    return problems


def all_problems():
    shape = shape_problems()
    if shape:  # the length check would trip over malformed messages
        return shape
    return ([p for memory in FORMATS for p in length_problems(memory)] + form_problems()
            + neutral_problems())


def check():
    """Raise if either format's histories are not matched; called before any run."""
    problems = all_problems()
    if problems:
        raise SystemExit("Histories are not matched:\n  " + "\n  ".join(problems))


def print_log_shape():
    """The log's size and shape, next to the StudyChat numbers it was written to match."""
    msgs = [m for s in cfg.LOG_SESSIONS for m in s["messages"]]
    nmsgs = [m for s in cfg.NEUTRAL_LOG_SESSIONS for m in s]
    nwords = [len(m[NEUTRAL].split()) for m in nmsgs]
    own = {k: [len((m.get("both") or m[k]).split()) for m in msgs] for k in KINDS}
    short = sum(n <= 3 for n in own[KINDS[0]])
    pasted = sum(any(key in m for key, _ in PASTE_KEYS) for m in msgs)
    per_session = [len(s["messages"]) for s in cfg.LOG_SESSIONS]
    words = sorted(n for k in KINDS for n, m in zip(own[k], msgs) if "both" not in m)
    pairs = [m for m in msgs if "both" not in m]
    questions = sum(sentence_form(m[KINDS[0]]) == "question" for m in pairs)
    npasted = sum(any(key in m for key, _ in PASTE_KEYS) for m in nmsgs)
    nrequests = sorted(n for n in nwords if n > 3)
    print(f"\n{'':<34}{'instr/exec':>12}{'neutral':>9}   StudyChat")
    print(f"{'messages':<34}{len(msgs):>12}{len(nmsgs):>9}   ~29 by week 6 (median 67 per semester)")
    print(f"{'messages per session':<34}{f'{min(per_session)}-{max(per_session)}':>12}"
          f"{f'{min(map(len, cfg.NEUTRAL_LOG_SESSIONS))}-{max(map(len, cfg.NEUTRAL_LOG_SESSIONS))}':>9}"
          "   median 4 per chat")
    print(f"{'3 words or fewer':<34}{f'{short / len(msgs):.0%}':>12}"
          f"{f'{sum(n <= 3 for n in nwords) / len(nmsgs):.0%}':>9}   5%")
    print(f"{'with pasted material':<34}{f'{pasted / len(msgs):.0%}':>12}{f'{npasted / len(nmsgs):.0%}':>9}"
          "   about 34% (code)")
    print(f"{'own words per request':<34}{f'{words[0]}-{words[-1]}':>12}"
          f"{f'{nrequests[0]}-{nrequests[-1]}':>9}   median 11, middle half 7-20")
    print(f"{'pairs as questions / commands':<34}{f'{questions} / {len(pairs) - questions}':>12}"
          f"{'':>9}   ~half questions, >=11% commands (rough)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--memory", default=cfg.MEMORY_FORMAT, choices=FORMATS)
    memory = p.parse_args().memory

    shape = shape_problems()
    if shape:
        sys.exit("Log template is malformed:\n  " + "\n  ".join(shape))

    texts = {k: build_memory(k, memory) for k in KINDS}
    for kind in KINDS:
        print(f"{'=' * 70}\n{kind} history ({memory})\n{'=' * 70}{texts[kind]}\n")

    print(f"{'=' * 70}\nLength per request pair (words)\n{'=' * 70}")
    print(f"{'':<22}{'instr':>7}{'exec':>6}{'diff':>6}  form")
    for label, r in request_pairs(memory):
        wi, we = len(r["instr"].split()), len(r["exec"].split())
        fi, fe = sentence_form(r["instr"]), sentence_form(r["exec"])
        form = fi if memory == "log" and fi == fe else "" if memory == "summary" else f"{fi} / {fe}  MISMATCH"
        print(f"{label:<22}{wi:>7}{we:>6}{we - wi:>+6}  {form}")
    for k in KINDS:
        print(f"{k:>6} total: {len(texts[k].split())} words, {len(texts[k])} characters")
    if memory == "log":
        print_log_shape()

    print(f"\n{'=' * 70}\nLine diff (only request lines should appear)\n{'=' * 70}")
    for line in difflib.unified_diff(texts["instr"].splitlines(), texts["exec"].splitlines(),
                                     "instr", "exec", n=0, lineterm=""):
        print(line)

    if memory == "log":
        neutral = build_log(NEUTRAL)
        print(f"\n{'=' * 70}\nneutral history (log, condition D_neutral)\n{'=' * 70}{neutral}\n")
        print(f"neutral total: {len(neutral.split())} words, {len(neutral)} characters")

    problems = length_problems(memory) + (form_problems() + neutral_problems() if memory == "log" else [])
    print("\nLength and form check: " + ("OK" if not problems else "FAILED\n  " + "\n  ".join(problems)))


if __name__ == "__main__":
    main()
