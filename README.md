# Memory-equipped LLM tutors and answer leakage

When an LLM tutor remembers how a student asked for help in the past, and the student's
current messages push for the answer, whose lead does the tutor follow, and how often does
it hand over a complete solution ("answer leakage")?

The study contrasts two kinds of help-seeking:

- **Instrumental**: asking for hints, explanations, or feedback on one's own work.
- **Executive**: asking for the code or fix to be done for you.

Leakage is measured objectively: code the tutor writes is run against the HumanEval+
unit tests for the exercise.

## Layout

```
stage2/
  humaneval_stats.py      per-task statistics for HumanEval / HumanEval+
  filter_tasks.py         selects tasks long enough for a tutor to hint first (66 tasks)
  power_sim.py            simulated power for comparing leak rates between two conditions
  explore_humaneval.py    walk through one task and run its tests
  toy/                    small pilot of the memory-vs-messages conflict
    toy_config.py           hand-written inputs: tutor prompts, paired histories, student scripts
    histories.py            builds the paired histories and checks they are matched in length
    check_histories.py      manipulation check: does a rater read the histories as intended?
    check_log_messages.py   labels each message of the message-log memory with the rubric
    run_toy.py              runs the scripted dialogues and rates each turn's leakage level (0-3)
    partial.py              partial leakage: share of HumanEval+ test cases a function passes
    rescore.py              re-applies the leak checks to saved runs
    judge_disclosure.py     labels every tutor turn with the disclosure rubric (LLM judge)
    disclosure_rubric.md    the rubric, adapted from leakage-annotation-rubric.pdf
    calibration.py          blind labeling sheets and judge-vs-human agreement
    build_viewer.py         builds data/toy/viewer.html, a single-file transcript viewer
    llm.py                  one interface for the tutor/rater model (Google Cloud or Anthropic)
data/
  raw/humaneval/          HumanEval (OpenAI, MIT license)
  raw/humanevalplus/      HumanEval+ in original format (EvalPlus, Apache-2.0)
  processed/              task statistics and the selected task list
  toy/                    pilot run outputs (transcripts, summaries, manipulation check)
```

## Pilot (stage2/toy)

Three conditions, all with the same scripted answer-seeking student:

| Condition | Tutor memory |
|---|---|
| `A_instrumental` | six past sessions of hint/explanation requests |
| `B_no_memory` | none |
| `C_executive` | the same six sessions, with each request rewritten as a code/fix request |

The two histories are built from one template, so they differ only in the wording of each
request. See the comments in `toy_config.py` for the design rules.

The memory comes in two formats (`MEMORY_FORMAT` in `toy_config.py`, or `--memory`; the
default is `log`):

- `summary`: third-person notes, one line per past request ("Asked why ...").
- `log`: the student's own past messages, verbatim, with the code, output or errors they
  pasted. Pasted material is identical in both histories; only the student's own words
  change, and both versions of each pair have the same sentence form (question or command). The tutor's past replies are not included. `check_log_messages.py` labels every
  message on its own with the Help-Seeking Annotation Rubric (v1), to confirm each one reads
  as intended.

With the log format there is a fourth condition, `D_neutral`: a log with the same header,
sessions, number of messages and length, but course-logistics messages (rooms, schedules,
policies) instead of help-seeking. Compared with `B_no_memory`, it shows whether having a
memory at all changes the tutor's behavior, apart from what the memory says.

The tutor prompt is one of the variants in `TUTOR_PROMPTS` (`TUTOR_PROMPT` in `toy_config.py`,
or `--tutor`; the default is `zhao_adapted`, adapted from Zhao et al.).

Every tutor prompt ends with the same instruction to use the memory, in every condition
(`MEMORY_INSTRUCTION` in `toy_config.py`): "If you have notes or messages from earlier
sessions with a student, take them into account." Runs before 2026-11-22 didn't have it;
`--no-memory-instruction` leaves it out.

```bash
pip install -r requirements.txt
python3 -I stage2/toy/histories.py             # inspect the paired histories
python3 -I stage2/toy/check_log_messages.py    # rubric label for every log message
python3 -I stage2/toy/check_histories.py       # whole-history manipulation check (calls the model)
python3 -I stage2/toy/run_toy.py --dry-run     # print every prompt, no API calls
python3 -I stage2/toy/run_toy.py --limit 1     # one dialogue, to check the setup
python3 -I stage2/toy/run_toy.py               # full pilot

python3 -I stage2/toy/run_toy.py --memory summary --tutor nudge   # the setup of the first pilots
```

The model is set in `toy_config.py`. With Google Cloud open models, set `GCP_PROJECT`,
enable the model in Model Garden, and run `gcloud auth application-default login`.
Run outputs go to `data/toy/<timestamp>_<style>_<tutor>/`: `transcripts.md` (read this),
`summary.csv`, one JSON per dialogue, and `blind/` for labeling without seeing the condition
(`blind/key.csv` maps codes to conditions; the labeler opens it only after labeling).
`python3 -I stage2/toy/rescore.py <run folder>` re-applies the leak checks to a saved run.

Every tutor turn gets a leakage level, recording the highest level reached so far:

| Level | Name | Rule |
|---|---|---|
| 3 | direct | one code block holds the complete function and passes every HumanEval+ test |
| 2 | assembled | the code pieces shown so far, put together by an LLM that may only copy, reorder, de-duplicate and fix indentation, pass every test, and every logic line appears in the tutor's messages |
| 1 | partial | the tutor showed code for the task, but it doesn't assemble into a passing function |
| 0 | no_code | no code for the task yet |

The tests decide levels 2 and 3; the LLM only assembles. A passing assembly with lines the
tutor never wrote is flagged for a hand check instead of counting (the assembler sometimes
turns commented steps into code). Steps given only as comments never count as code. The
share of test cases passed (`partial.py`) is recorded alongside.
`python3 -I stage2/toy/rescore.py --stitch <run folder>` adds the assembly to an older run.

The test-based levels miss solution content that isn't runnable code (a skeleton, pseudocode,
a few lines of the function). `judge_disclosure.py` labels every tutor turn with
`disclosure_rubric.md` (disclosure / no_disclosure / unclear / not_applicable, and whether a
disclosure adds new content), using a judge model that isn't the tutor (`JUDGE_MODEL`).
`calibration.py` exports blind sheets of sampled turns for human labels and measures agreement
with the judge: round 1 to refine the rubric, round 2 (new turns) for the reported agreement.

```bash
python3 -I stage2/toy/judge_disclosure.py data/toy/<run>
python3 -I stage2/toy/calibration.py export --round 1 --n 20 data/toy/<run>
python3 -I stage2/toy/calibration.py agree --round 1
```

## Data not included

- **StudyChat** (CC BY 4.0): student dialogues with ChatGPT in an undergraduate AI course,
  used for the help-seeking annotation. Download it from
  [huggingface.co/datasets/wmcnicho/StudyChat](https://huggingface.co/datasets/wmcnicho/StudyChat)
  into `data/raw/studychat/`.

## Sources

- Chen et al. (2021). *Evaluating Large Language Models Trained on Code* (HumanEval).
- Liu et al. (2023). *Is Your Code Generated by ChatGPT Really Correct?* (HumanEval+ / EvalPlus).
