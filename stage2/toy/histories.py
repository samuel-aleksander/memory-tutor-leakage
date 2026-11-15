"""Build the paired memory histories from toy_config.SESSIONS and check they are matched.

Both histories come from one template, so structure (sessions, weeks, exercises, order,
number of requests, header) is identical by construction. This module checks what the
template can't guarantee: that each request pair, and the histories as a whole, have
about the same length.

Usage (from the project root):
    python3 -I stage2/toy/histories.py      # print both histories, the length table and a diff
"""

import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir
import toy_config as cfg  # noqa: E402

KINDS = ("instr", "exec")


def build_history(kind):
    """Memory block for one kind of history ("instr" or "exec")."""
    lines = [cfg.HISTORY_HEADER]
    for i, s in enumerate(cfg.SESSIONS, 1):
        lines.append(f"\nSession {i} (week {s['week']}), exercise `{s['exercise']}`:")
        lines += [f"- {r[kind]}" for r in s["requests"]]
    return "\n".join(lines)


def length_problems():
    """List every request pair or history that breaks the length tolerances."""
    problems = []
    for i, s in enumerate(cfg.SESSIONS, 1):
        for j, r in enumerate(s["requests"], 1):
            if set(r) != set(KINDS) or not all(r[k].strip() for k in KINDS):
                problems.append(f"session {i} request {j}: needs non-empty 'instr' and 'exec'")
                continue
            diff = abs(len(r["instr"].split()) - len(r["exec"].split()))
            if diff > cfg.MAX_PAIR_WORD_DIFF:
                problems.append(f"session {i} request {j}: {diff} words apart "
                                f"(max {cfg.MAX_PAIR_WORD_DIFF})")
    a, b = (len(build_history(k)) for k in KINDS)
    rel = abs(a - b) / max(a, b)
    if rel > cfg.MAX_TOTAL_LENGTH_DIFF:
        problems.append(f"histories differ in length by {rel:.1%} "
                        f"(max {cfg.MAX_TOTAL_LENGTH_DIFF:.0%})")
    return problems


def check():
    """Raise if the histories are not matched; called before any run."""
    problems = length_problems()
    if problems:
        raise SystemExit("Histories are not matched:\n  " + "\n  ".join(problems))


def main():
    for kind in KINDS:
        print(f"{'=' * 70}\n{kind} history\n{'=' * 70}{build_history(kind)}\n")

    print(f"{'=' * 70}\nLength per request pair (words)\n{'=' * 70}")
    print(f"{'session':>8}{'req':>5}{'instr':>7}{'exec':>6}{'diff':>6}")
    for i, s in enumerate(cfg.SESSIONS, 1):
        for j, r in enumerate(s["requests"], 1):
            wi, we = len(r["instr"].split()), len(r["exec"].split())
            print(f"{i:>8}{j:>5}{wi:>7}{we:>6}{we - wi:>+6}")
    texts = {k: build_history(k) for k in KINDS}
    for k in KINDS:
        print(f"{k:>6} total: {len(texts[k].split())} words, {len(texts[k])} characters")

    print(f"\n{'=' * 70}\nLine diff (only request lines should appear)\n{'=' * 70}")
    for line in difflib.unified_diff(texts["instr"].splitlines(), texts["exec"].splitlines(),
                                     "instr", "exec", n=0, lineterm=""):
        print(line)

    problems = length_problems()
    print("\nLength check: " + ("OK" if not problems else "FAILED\n  " + "\n  ".join(problems)))


if __name__ == "__main__":
    main()
