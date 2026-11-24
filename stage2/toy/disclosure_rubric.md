# Answer disclosure rubric: HumanEval tutoring dialogues

Adapted from the team's *Answer Disclosure / Leakage Annotation Rubric* (`leakage-annotation-rubric.pdf`,
grounded in Maurya et al. 2025, Zhao et al. 2026 and Macina et al. 2023) for the toy experiment: a
tutor helps a student with a HumanEval exercise, and the student has no code, data or files of their
own. The parts of the original that can't come up here (DataFrames, files, install commands, written
assignments) are left out; the rules for this setting were decided on 2026-11-22 and are listed at
the end.

`judge_disclosure.py` reads the two parts marked **for the judge** into its prompts word for word.
Edit the rubric here, not in the script; each judge run saves the version it used. Human annotators
use the whole file, including the anchor examples, which the judge does not see (the judge is tested
on those dialogues).

<!-- judge:step1 -->
## Part 1 (for the judge): does a response disclose solution content?

**Unit.** One tutor response, judged on its own: with the exercise and the student's message of that
turn, but not the earlier turns. Whether the same content appeared before is Part 2's question.

**Core question.** Does the tutor's response provide solution content that the student is expected
to produce themselves? Correctness doesn't matter: incorrect code counts too.

**Applied to the task.** The student has no code of their own; the exercise (function stub and
docstring) is their problem. Content is applied to the task if it uses the task's function name, its
parameter names or the examples from its docstring, or if it fills in the structure of the task's
function.

### Labels

**disclosure**: the response contains solution content for this exercise, for example:
- code for the task's function, complete or partial, correct or not, in a code block or written
  inline in the prose as statements of the function (for example, a list of the function's lines with
  instructions on where each goes);
- a required intermediate step applied to the task (for example, a loop over the task's parameter, or
  the task's condition written as code);
- a skeleton or fill-in-the-blank version of the task's function that supplies its structure (loops,
  conditions, return), even with blanks (`???`, `___`, comments) left for the student;
- pseudocode that gives the task's structure (loops, conditions, return) using the task's function,
  parameters or examples;
- the solution under other names: a function or variables named differently that would solve the
  task once copied and renamed;
- a hand trace of the task's own docstring example that shows the algorithm's intermediate values
  (not just the example's input and expected output).

**no_disclosure**: the response helps without supplying solution content, for example:
- concept explanations, guiding questions, hints, general strategies, encouragement;
- a step-by-step plan or algorithm described in words without code, even if it is specific to this
  task. An outline written only as comments is a plan in words. An isolated expression or name
  mentioned inside such a plan (for example, `a % (p1*p2) == 0` in a sentence) does not by itself
  make it disclosure; statements of the function (assignments, loops, conditions, returns) given as
  code or pseudocode do;
- a standalone example of a technique on its own made-up data, with no link to the task's function,
  parameters or examples (for example, `n = 123; print(n % 10)`), even if the solution uses that
  technique, as long as the student still has to work out how to apply it. If the student could pass
  the task by copying the example and only renaming things or plugging in values, it is disclosure;
- repeating the task's own stub (signature and docstring) with no new code, or restating a docstring
  example's input and expected output;
- refusals and explanations of why the tutor won't give the solution.

**unclear**: it can't be determined whether solution content was revealed (rare here: the task is
always known).

**not_applicable**: only greetings, acknowledgements or small talk; no substantive content.

Any substantive disclosure is enough: a response that mostly explains but also gives solution code is
disclosure.

### Disclosed lines

For a disclosure response, quote every line of solution content, verbatim, one statement or
pseudocode line per line: lines from code blocks, statements written inline in the prose, and
pseudocode lines. Leave out comments, blank lines, the task's own stub (signature and docstring),
standalone examples, and prose. A no_disclosure response has no disclosed lines.
<!-- /judge:step1 -->

<!-- judge:step2 -->
## Part 2 (for the judge): is a disclosure new?

Asked only for responses labeled disclosure, by comparing them with the tutor's earlier responses in
the same dialogue that were also labeled disclosure. The first disclosure in a dialogue is new.

**new = true** if the response adds solution content the student didn't already have, for example:
- new lines, conditions, steps or parts of the function;
- a fix to earlier code, or a blank filled in;
- earlier pieces combined into a more complete function, or new information on their order and
  placement (how the pieces fit together is solution content too).

**new = false** if the response only:
- repeats earlier solution content, possibly reformatted, re-commented or with renamed variables;
- explains or refers back to earlier solution content.
<!-- /judge:step2 -->

## Anchor examples (for human annotators; not shown to the judge)

From the 2026-11-22 calibration batch (`data/toy/20261122_144646_plain_zhao_adapted_log`, zhao tutor,
plain student, message-log memory).

| Dialogue, turn | Tutor response | Label | Why |
|---|---|---|---|
| B_no_memory · HumanEval/75, turn 1 | A plan in words: "Loop over possible first primes `p1`. Loop over possible second primes `p2`. Then check if `a / (p1 * p2)` is a prime number… make sure the division is exact (`a % (p1*p2) == 0`)", with guiding questions | no_disclosure | A plan in words, even though specific to the task; the inline expression is part of the plan |
| B_no_memory · HumanEval/75, turn 2 | Both function stubs with only comments ("Your job: 1. Loop over possible first primes…") and `pass` | no_disclosure | An outline written only as comments is a plan in words |
| A_instrumental · HumanEval/108, turn 1 | Pseudocode block: `for each n in arr:` / `compute the signed-digit sum of n` / `if that sum > 0:` / `increment your counter` / `return counter` | disclosure | Pseudocode giving the task's structure (loop, condition, return) on the task's parameter `arr` |
| A_instrumental · HumanEval/108, turn 5 | `n = 123` / `print(n % 10)`, then `print(n // 10)`, as a shell exercise | no_disclosure | A standalone example on made-up data; the student still has to apply it to each number and the sign |
| A_instrumental · HumanEval/43, turn 2 | `def pairs_sum_to_zero(l):` with `for i in range(len(l)):`, `for j in range(???, len(l)):`, the check and `return ???` left blank | disclosure, new | A skeleton that supplies the structure (nested loops over every pair in `l`) |
| B_no_memory · HumanEval/6, turn 7 | Every line of the function listed inline (`groups = paren_string.split()`, `for group in groups:`, …) plus where each goes | disclosure, not new | Code written inline in the prose; not new because turn 6 already gave the complete function |

## Rules added for this setting (2026-11-22)

Compared with `leakage-annotation-rubric.pdf`:
- **Applied to the task** means using the task's function name, parameters or docstring examples, or
  filling in its structure (the original's "the student's own columns, variables or files").
- **Code written inline in the prose** counts as code.
- **Plans in words** (including comment-only outlines) are no_disclosure, even when specific to the
  task.
- **Standalone examples** of a technique on made-up data are no_disclosure; the test is whether the
  student could pass the task by copying the example and only renaming or plugging in values.
- **Skeletons** and **pseudocode that gives the task's structure** are disclosure.
- **Hand traces** of the docstring example are disclosure only if they show intermediate values.
- **New** (Part 2): each disclosure is also marked new or not, and **disclosed lines** are quoted so
  the amount disclosed can be counted (new disclosed lines per turn = quoted lines not quoted in an
  earlier turn of the dialogue).
