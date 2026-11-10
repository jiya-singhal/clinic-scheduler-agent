# Clinic scheduling agent with a self-improving eval loop

A voice-style patient appointment scheduling agent (Gemini, native function calling, typed state outside the model, enforcement in the tools) plus an evaluation harness that scores 14 scenarios with programmatic trace checks and a cross-family LLM judge, then closes the loop: it turns the most common failure into one structured prompt rule, re-runs every scripted scenario, and accepts the new prompt only if a five-condition regression gate passes. **Result: v1 0.930 -> v2 0.985, 12 of 12 scripted scenarios passing, and the learned rule also fixed an unseen simulated scenario it was never shown.** The clean-clone rerun also accepted a second rule that fixed the eager week-wide availability fetch, a failure nobody seeded: that is the loop finding something unknown to its author (v1 0.932 -> v3 0.979 -> v4 0.983, in [`history/clean-clone-run/`](history/clean-clone-run/)).

## Setup

```
git clone <repo> && cd <repo>
uv sync
cp .env.example .env     # fill GEMINI_API_KEY and ANTHROPIC_API_KEY
```

That is all. Python 3.12, no web framework, no database.

## The two commands

```
uv run python -m agent.chat
uv run python -m evals.run --loop --start prompts/v1.md --max-iterations 3 --auto-apply
```

The first is an interactive chat with the agent (Ctrl-D or `quit` to exit). The second evaluates the start prompt, proposes one rule, writes the next free prompt version, re-evaluates all scripted scenarios, applies the regression gate, and writes `history/loop.md`. Without `--auto-apply` it prints the proposed rule and waits for `y/n` before applying it. On a fresh clone the committed `prompts/v2.md` already exists, so a live run writes `prompts/v3.md`; the committed v2 is the recorded run. A report is only reused when its traces are on disk, and `runs/` is not committed, so a fresh clone re-evaluates v1 first (about 10 minutes).

Example session with the learned prompt (`--prompt prompts/v2.md`; patient lines are echoed when input is piped):

```
patient> Hi, I'd like to book an appointment.
agent> I can certainly help with that. Could you please share your full name and date of birth?

patient> Maria Gonzalez, 14 March 1985.
agent> Thanks, Maria. What is the reason for your visit, and do you have a preferred day or doctor?

patient> A check-up, Wednesday the 14th morning, Dr. Lee.
agent> Dr. Lee has openings on Wednesday, October 14th starting at 9:00 AM, with times like 9:30 and 10:00 AM. Would any of those work for you?

patient> The 9:30 please.
agent> I have you down for Dr. Marcus Lee on Wednesday, October 14th at 9:30 AM for a check-up. Would you like me to go ahead and book that?

patient> Yes, that's right.
agent> You're all booked with Dr. Marcus Lee on Wednesday, October 14th at 9:30 AM. Is there anything else I can help you with today?
```

Secondary commands:

```
uv run python -m evals.run --prompt prompts/v1.md --out reports/v1.json          # one full eval, writes .json and .md
uv run python -m evals.run --prompt prompts/v1.md --out reports/x.json --only slot-taken-race --no-judge
uv run python -m agent.chat --script scenarios/smoke.json                        # scripted replay
uv run python -m agent.chat --prompt prompts/v2.md                               # chat with the learned prompt
uv run pytest -v                                                                 # 78 tests, no API calls
uv run --env-file .env python -m evals.judge_audit --out evals/judge_audit/      # judge bias audit, see below
```

## Architecture

```
patient turn
    |
    v
Agent.turn ──> Gemini (tools, temp 0) ──> function calls ──> tools.dispatch ──> Clinic backend
    ^              system prompt +            |                 (slot_not_offered     (verified set, idempotent
    |              serialised STATE           |                  control)              booking, slot_taken)
    |                                         v
    |                              state.apply(tool, args, result)   <- the ONLY place state mutates
    |                                         |
    └───── assistant text ◄───────────────────┘        every step -> runs/<ts>.jsonl trace

evals.run ──> run_scenario (scripted turns or Gemini-simulated patient, optional fault injection)
                  |
                  ├─> checks.py   layer 1: pure functions over the trace (gates: PHI, forbidden booking, escalation)
                  ├─> judge.py    layer 2: Claude Opus, structured JSON, sees transcript + layer 1 results
                  └─> score.py    0 if a gate fails, else 0.6 * layer1 pass rate + 0.4 * judge mean

--loop: evaluate -> improver.py (one rule for the single highest-count failed check)
        -> versions.py (append-only, provenance comment, sha256)
        -> evaluate again -> gate.py (five conditions) -> accept or stop
```

- **State lives outside the model.** `ConversationState` is a dataclass; `apply()` runs only after a tool executes and may read the call's arguments and result. No tool call, no state change, and there is a test for exactly that.
- **Enforcement is tool-side.** Booking needs a patient verified in this session, a taken slot returns `slot_taken`, a repeated identical booking returns the same appointment id, and dispatch rejects any slot id the tool never offered. The prompt is advice; the backend is law.
- **Two scoring layers.** Layer 1 reads the trace and can see what the judge cannot (did the booking land, was verification a real tool call, was a slot id real). Layer 2 grades what no regex can (clarity, tone, medical advice). `evals/JUDGE_LIMITS.md` lists what the judge is blind to and which check covers each.
- **One rule per iteration, append only.** The improver may only add a rule; it may not touch existing text, name scenarios, or exceed 400 characters. Every version carries a provenance comment and its parent's hash.
- **The gate reads checks, not just scores.** Target check pass count must rise, no scenario may drop more than 0.03, no previously passing check may fail, no safety gate may fail, the mean may not fall.

## Repo map

```
agent/        backend.py (clinic + enforcement), tools.py (schemas + dispatch), state.py, loop.py (Gemini loop + trace), chat.py (CLI)
evals/        checks.py, judge.py, score.py, simulate.py, improver.py, versions.py, gate.py, run.py, JUDGE_LIMITS.md, judge_audit.py
evals/judge_audit/   REPORT.md, results.json, human_sheet.md (+ key), score_human.py, traces/claude-v2/ (second answer family)
scenarios/    14 scenario files (12 scripted, 2 simulated) plus smoke.json
prompts/      v1.md (hand-written), v2.md (v1 + learned rule R-1)        committed: evidence
reports/      v1.json/.md, v2.json/.md, v2-simulated.json/.md           committed: evidence
history/      loop.md, versions.json, attempt-1..3/ (rejected runs), clean-clone-run/, accepted-run-traces/ (replayable JSONL for v1, v2, v2-simulated)   committed: evidence
data/         seed.json (2 providers, 5 patients, 10 business days, fixed today 2026-10-12)
tests/        78 pytest tests, all offline (fake model, fake judge)
runs/         JSONL traces, gitignored
DECISIONS.md  every design choice, proposals vs human calls, index at top
ASSUMPTIONS.md, DESIGN_NOTE.md
```

## Judge audit

The layer 2 judge is a model too, so it gets its own eval: `evals/judge_audit.py`, following Zheng et al. 2023 ("Judging LLM-as-a-judge"). It measures three known judge biases on saved transcripts, applies three fixes, and measures again. Opus (`claude-opus-5-5`, the production judge, a different family from the agent) and Gemini (the agent's own family) both judge. The Gemini judge runs on `gemini-3.5-flash-lite`: `gemini-3.8-flash`, the agent's model, returned 503 on every probe on both run days, and `gemini-3.5-flash` stopped at the free tier's 20 requests a day (D43).

- **Position.** A pairwise judge sees the scenario brief, two transcripts and the same six criteria as `evals/judge.py`, and picks A, B or tie with a reason. Every pair is judged in both orders. A verdict that changes when the two transcripts swap places is position bias. Pairs: v1 vs v2 on the 12 scripted scenarios, and Gemini agent vs Claude agent on the same 12.
- **Length.** Each v2 transcript gets a padded copy: every assistant line gains filler that restates the patient's last line and adds nothing. Facts stay identical. The existing single-transcript judge grades both copies with the same layer 1 results, and the pairwise judge compares them in both orders.
- **Self-preference.** The 12 scripted scenarios are rerun with the v2 prompt on a Claude agent (`claude-haiku-4-5` at temperature 0, through `anthropic_model()` in `agent/loop.py`). Both judges compare Gemini agent and Claude agent in both orders. Raw pick rates mix quality with bias, so the signal is the gap between the two judges on the same pairs.
- **Fixes.** (i) A win counts only if it holds in both orders, otherwise tie. (ii) The rubric gains "Length and verbosity are not quality; do not reward restatement." (iii) The judge is from a different family than the agent.

Headline (Opus judge run 2026-11-09, Gemini judge run 2026-11-10; every number is in `evals/judge_audit/results.json` and `REPORT.md`):

> Judge agreed with me on X/N: pending human sheet. Swapping answer order changed 3 of 12 Opus verdicts on v1 vs v2 and 4 of 12 on Gemini agent vs Claude agent; for the Gemini judge 3 of 12 and 7 of 12, every one a full reversal. Judging in both orders and counting only consistent wins removes order effects by construction and leaves 5 of 12 v1 vs v2 verdicts decisive for Opus, all for v2, and 9 of 12 for Gemini, 5 for v2 and 4 for v1. The padded copy outscored the original on 0 of 12 transcripts before the verbosity rubric line and 0 of 12 after, under both judges.

- **Position.** Of the 7 verdicts that changed with order, 6 were a win in one order and a tie in the other. One was a full reversal (Gemini vs Claude, no-slots-in-range): Opus picked whichever transcript was shown second, calling it "slightly better" both times for different reasons. The Gemini judge never said tie: all 10 of its order changes were full reversals, and on Gemini agent vs Claude agent it picked the transcript shown second in 7 of 12 pairs, both ways round. The both-orders rule turns those into ties and leaves it 5 of 12 decisive. `REPORT.md` quotes flipped verdicts from both judges.
- **Length.** Both judges penalised the padding every time. The Opus single-transcript mean fell from 0.958 to 0.794, almost all of it on clarity (-0.575), and the padded copy lost all 24 pairwise verdicts under each judge. The rubric line had nothing to fix. Blatant filler is the easy case; this does not show the judge resists subtler verbosity.
- **Self-preference.** Opus picked the Gemini agent in 17 of 24 single-order verdicts and the Claude agent, its own family, in 4 (3 ties); under the both-orders rule 7, 1 and 4. Layer 1 points the same way (Gemini v2 passes every check in 12 of 12 scenarios, the Claude agent in 10 of 12), so this reads as quality, with no sign of Opus favouring Claude. The Gemini judge, the agent's own family, also picked the Gemini agent in 17 of 24 single-order verdicts, and the Claude agent in 7 (no ties); under the both-orders rule 5, 0 and 7. The two judges land on the same raw rate, so there is no self-preference gap to see here. The comparison is weak anyway: the judge is a sibling model of the agent, not the agent's model, and the two agents differ in quality.
- **Validity.** On v1 vs v2, Opus picked v2 in 10 of 24 single-order verdicts, v1 in 3, and tie in 11. 7 of its 24 reasons call the read-back turn extra or redundant, including 2 of the 3 picks for v1, although the loop added that turn on purpose. The Gemini judge picked v2 in 13 of 24 and v1 in 11, never tie; under the both-orders rule 5, 4 and 3. A third of its consistent verdicts go against the rule the loop optimised for, so it would be a poor gate for this loop.

```
uv run --env-file .env python -m evals.judge_audit --out evals/judge_audit/   # resumable: each call is saved as it lands
uv run python -m evals.judge_audit --sheet-only --out evals/judge_audit/      # human sheet from saved traces, no model calls
uv run python evals/judge_audit/score_human.py                                # after filling evals/judge_audit/human_sheet.md
uv run --env-file .env python -m evals.judge_audit --judges compat --out evals/judge_audit/   # free judge, see below
```

`REPORT.md` in the same folder has every table: position flips per judge before and after the both-orders rule, length bias before and after the rubric line, self-preference rates with layer 1 results for context, and two real verdicts that flipped with order with both of the judge's reasons. The human sheet holds the 24 pairs shuffled and blinded as X and Y, with the mapping in `human_sheet_key.json`. The Opus part used 144 calls, 294,954 input and 39,149 output tokens, about 2 dollars at list price; the 12 Claude-agent runs on Haiku 4.5 add cents. The Gemini judge used 96 calls, 101,468 input and 5,556 output tokens, on the free tier. Two abandoned starts are also in the cache and in no table: 18 calls on gemini-3.5-flash (stopped by the daily free-tier cap) and 47 of 96 calls on a free OpenRouter model, Nemotron 3 Ultra (`--judges compat`, stopped by OpenRouter's daily cap for free models). The fixes are measured, not adopted: `evals/judge.py` is unchanged (D43 to D45).

The sheet is append-only. `--sheet-only` writes the 12 v1 vs v2 pairs from committed traces now; the Gemini vs Claude pairs are appended as H13 to H24 once their traces exist, and ids, labels and filled answers never change. Opus is the documented judge. `--judges compat` adds a judge that can cost nothing: any OpenAI-compatible endpoint, set with `OPENAI_COMPAT_MODEL` and `OPENROUTER_API_KEY` (or `OPENAI_COMPAT_BASE_URL` and `OPENAI_COMPAT_API_KEY`). Its model is recorded in `results.json` and in every cache key (D46).

## Cost and runtime (estimates)

Estimated from call counts, not from billing. One full eval of 12 scripted scenarios takes about 8 to 10 minutes and roughly 60 Gemini calls plus 12 Opus judge calls, about $0.40 to $0.60. One loop run (baseline reused, one new version, simulated on the final) is about 15 minutes and under $1. The whole project, including three rejected loop runs and Phase 2 reruns, cost in the 6 to 8 dollar range, judge calls being the larger share.

## Known limits and future work

- The agent fetches availability for the whole current week right after verification, before the patient says when. v2 happened to pass the check that catches this, so the recorded run never targeted it. The clean-clone rerun did target it in iteration 2 and accepted rule R-2 (`history/clean-clone-run/prompts/v4.md`), but v2 remains the recorded prompt.
- On a rejected version the loop stops. Retrying with a different rule, or targeting two checks in one iteration, is not implemented.
- Offering two doctors at the same time ("1:00 PM with either Dr. Patel or Dr. Lee") makes "the first one" ambiguous for voice. The judge flags it; no layer 1 check measures it, so the improver cannot target it.
- Temperature 0 is not determinism. The same v1 prompt scored 0.918, 0.930, 0.933 and 0.939 across four runs. The gate therefore reads check results and traces, not only scores.
- Scripted scenarios cannot answer a question the agent did not ask in v1. A two-line deterministic stand-in ("Yes, that's right" or "The first one, please") fills that gap after the script ends. Production evaluation should use simulated patients with many runs per scenario.
- Phase 3 took three rejected runs to get right; every rejection was a harness defect caught by the gate, recorded in `history/attempt-*` and DECISIONS.md D37 to D39.
