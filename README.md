# Clinic scheduling agent with a self-improving eval loop

A voice-style patient appointment scheduling agent (Gemini, native function calling, typed state outside the model, enforcement in the tools) plus an evaluation harness that scores 14 scenarios with programmatic trace checks and a cross-family LLM judge, then closes the loop: it turns the most common failure into one structured prompt rule, re-runs every scripted scenario, and accepts the new prompt only if a five-condition regression gate passes. **Result: v1 0.930 -> v2 0.985, 12 of 12 scripted scenarios passing, and the learned rule also fixed an unseen simulated scenario it was never shown.** The clean-clone test re-ran the loop from scratch and accepted two rules, v1 0.932 -> v3 0.979 -> v4 0.983, the second one fixing the eager availability fetch; that run is in `history/clean-clone-run/`.

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

Secondary commands:

```
uv run python -m evals.run --prompt prompts/v1.md --out reports/v1.json          # one full eval, writes .json and .md
uv run python -m evals.run --prompt prompts/v1.md --out reports/x.json --only slot-taken-race --no-judge
uv run python -m agent.chat --script scenarios/smoke.json                        # scripted replay
uv run python -m agent.chat --prompt prompts/v2.md                               # chat with the learned prompt
uv run pytest -v                                                                 # 61 tests, no API calls
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
evals/        checks.py, judge.py, score.py, simulate.py, improver.py, versions.py, gate.py, run.py, JUDGE_LIMITS.md
scenarios/    14 scenario files (12 scripted, 2 simulated) plus smoke.json
prompts/      v1.md (hand-written), v2.md (v1 + learned rule R-1)        committed: evidence
reports/      v1.json/.md, v2.json/.md, v2-simulated.json/.md           committed: evidence
history/      loop.md, versions.json, attempt-1..3/ (rejected runs), clean-clone-run/   committed: evidence
data/         seed.json (2 providers, 5 patients, 10 business days, fixed today 2026-10-12)
tests/        61 pytest tests, all offline (fake model, fake judge)
runs/         JSONL traces, gitignored
DECISIONS.md  every design choice, proposals vs human calls, index at top
ASSUMPTIONS.md, DESIGN_NOTE.md, RECORDING.md
```

## Cost and runtime (estimates)

Estimated from call counts, not from billing. One full eval of 12 scripted scenarios takes about 8 to 10 minutes and roughly 60 Gemini calls plus 12 Opus judge calls, about $0.40 to $0.60. One loop run (baseline reused, one new version, simulated on the final) is about 15 minutes and under $1. The whole project, including three rejected loop runs and Phase 2 reruns, cost in the 6 to 8 dollar range, judge calls being the larger share.

## Known limits and future work

- The agent fetches availability for the whole current week right after verification, before the patient says when. v2 happened to pass the check that catches this, so the recorded run never targeted it. The clean-clone rerun did target it in iteration 2 and accepted rule R-2 (`history/clean-clone-run/prompts/v4.md`), but v2 remains the recorded prompt.
- On a rejected version the loop stops. Retrying with a different rule, or targeting two checks in one iteration, is not implemented.
- Offering two doctors at the same time ("1:00 PM with either Dr. Patel or Dr. Lee") makes "the first one" ambiguous for voice. The judge flags it; no layer 1 check measures it, so the improver cannot target it.
- Temperature 0 is not determinism. The same v1 prompt scored 0.918, 0.930, 0.933 and 0.939 across four runs. The gate therefore reads check results and traces, not only scores.
- Scripted scenarios cannot answer a question the agent did not ask in v1. A two-line deterministic stand-in ("Yes, that's right" or "The first one, please") fills that gap after the script ends. Production evaluation should use simulated patients with many runs per scenario.
- Phase 3 took three rejected runs to get right; every rejection was a harness defect caught by the gate, recorded in `history/attempt-*` and DECISIONS.md D37 to D39.
