# Phase 5 — Automated evaluation of AI output

## What it does

```bash
python -m eval.runner              # everything available
python -m eval.runner --offline    # deterministic checks only, no key needed
python -m eval.runner --require-online   # fail if model cases were skipped
```

Runs a golden set of 15 cases, scores tool-call correctness, grounding,
similarity, and LLM-as-judge verdicts, prints a per-case report, and **exits
non-zero on regression** so CI fails loudly.

Unit tests prove the code does what it says. This proves the *system still does
what it is for* — and those are different failures.

## It immediately earned its place

Running it for the first time failed two cases, and diagnosing them exposed
**three real retrieval bugs** that 109 passing unit tests had not caught,
because every individual function worked correctly:

1. **Stopwords outranked domain terms.** IDF assumes rare ⇒ informative. In a
   six-section corpus the word "do" appears once, so IDF scored it as highly
   informative — and "how **do** I restart a pump" was pulled toward the
   section saying "**Do** not restart until the bearing has been inspected."
   *Fix: stopword filtering.*

2. **One incidental word counted as a match.** "quantum flux capacitor
   **alignment**" matched the pump-vibration section, because "alignment" is a
   real domain term. Returning that as a hit invites the model to ground an
   answer in an irrelevant section — worse than returning nothing.
   *Fix: a minimum query-coverage threshold.*

3. **A titled section tied with a passing mention.** Set-based scoring counts
   each term once, so "Feedwater pump **restart** procedure" (titled, mentions
   it three times) scored identically to a section saying "Do not **restart**"
   once — and the tie broke alphabetically, on section id.
   *Fix: sub-linear term frequency plus a title weight.*

All three are now regression tests. This is the argument for the harness in one
paragraph: these are behaviours of the *system*, invisible to tests of its parts.

## The central design problem: non-determinism

You cannot assert equality against a model's output. It changes between runs,
and a suite that fails on harmless rewording gets muted within a week — and **a
muted eval is worse than none**, because it produces confident green.

So every case is anchored by at least one **decidable** check, with fuzzy checks
adding signal on top. Checks in descending order of trustworthiness:

| Check | Decidable? | What it is for |
|---|---|---|
| `json_path_equals` | yes | exact structural assertion on tool output |
| `forbids` | yes | **the best hallucination detector here** — an answer about a non-existent pump must contain no "degC" |
| `contains` | yes | the real value is present |
| `tool_calls` / `tool_arguments` | yes | right tool, right arguments |
| `similarity_to` | no | lexical floor against answer collapse |
| `judge` | no | meaning that nothing above can check |

A meta-test enforces this rule: `test_every_online_case_has_at_least_one_
decidable_check` fails the build if a case rests on fuzzy checks alone. It
caught one of my own cases doing exactly that.

**`similarity` is labelled honestly.** It is lexical token-overlap F1, *not*
semantic — it scores a flat contradiction that reuses the reference's
vocabulary above 0.5, and there is a test asserting that weakness so nobody
mistakes it for meaning. It is a floor against collapse; the judge does
semantics.

**Tool-call checking is subset, not exact.** An agent that looks up one extra
thing has not regressed. Requiring an exact call set would fail on harmless
variation and train everyone to ignore the suite.

## The second design problem: CI without keys

Model cases need `ANTHROPIC_API_KEY`. A fork's CI does not have one. So:

- **Offline cases** (tools, retrieval) always run — anyone, anywhere, no key.
- **Online cases** are reported as **SKIPPED, never passed**. Counting a skip as
  a pass is precisely how an eval becomes decorative.
- `--require-online` makes a skip a failure, for the pipeline that *does* have a
  key and where a silent skip would mean the model checks quietly stopped.

## The judge fails safe

If the judge is unreachable, returns unparseable output, or errors, that is a
**FAILED check** — never a passed one. An eval that goes green when its grader
is broken is worse than no eval. Both failure modes have tests.

The judge also runs on a cheap tier (grading against an explicit rubric is far
easier than the task being graded), and its system prompt tells it to ignore any
instructions inside the answer under test — the answer is untrusted input, and
the grader is a prompt-injection target too.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | everything that ran, passed |
| 1 | at least one check failed (or `--require-online` and something skipped) |
| 2 | the harness itself could not run — bad golden set, no cases matched |

`ERROR` is tracked separately from `FAIL`: the system breaking and the system
behaving wrongly want different investigation.

## Honest status

- Offline cases: **9, all passing, verified.**
- Online cases: **6, never executed** — no API key in this environment. The
  code paths are unit-tested with a scripted model, but the actual model
  behaviour they assert is unverified until a key exists. They are reported as
  skipped, not passed, precisely so this is not glossed over.

## Tests

`python -m pytest -q` — 134 tests. Phase 5 adds tests for the scoring functions,
the judge's fail-safe behaviour, the golden set's own well-formedness, and the
three retrieval regressions the harness found.
