# DECISIONS

Design log for the scheduling-agent take-home. Every entry is tagged:

- `[DIRECTIVE]` given in the brief, recorded with the reasoning.
- `[PROPOSAL]` AI-proposed, awaiting human decision. Treat as not final.
- `[DECIDED]` human confirmed or overrode. The override and why go in the entry.
- `[ASSUMPTION]` ambiguity call; details live in ASSUMPTIONS.md.

The brief asks us to show where AI helped and where human judgment overrode it,
so proposals stay proposals until the reviewer marks them.

## D1. Model split: Gemini agent, Claude judge `[DIRECTIVE]`

Agent is Gemini 2.5 Flash (google-genai, native function calling, temperature 0).
Judge in Phase 2 is Claude via the anthropic SDK.

Why different families: a judge from the same family as the agent shares its
blind spots and stylistic preferences, so it tends to grade its own failure modes
as acceptable. A cross-family judge gives a less correlated error signal. The
cost is two API keys and two SDKs, which is small.

Temperature 0 so the harness is as deterministic as the vendor allows. We will
note in the design doc that Gemini at temperature 0 is not bit-for-bit
deterministic and the eval reports should be read with that in mind.

## D2. Module layout `[DECIDED]`

```
agent/
  __init__.py
  backend.py    Clinic: in-memory store, seed loading, slot generation, all enforcement
  tools.py      the 5 tool functions, their JSON schemas, dispatch(name, args)
  state.py      ConversationState dataclass + apply(state, call, result)
  loop.py       system prompt, state serialisation, one-turn agent loop, JSONL trace
  chat.py       CLI: interactive, or --script scenarios/x.json
data/
  seed.json     providers, patients, anchor date, hours, pre-taken slots
scenarios/
  smoke.json    scripted patient turns for the smoke run
tests/
  test_backend.py   tool-side enforcement
  test_state.py     state only moves via tool results; never-offered slot rejected
runs/           JSONL traces, gitignored
```

No `evals/` package yet. Phase 2 creates it. Nothing is scaffolded for later.

Why five modules and not one: backend enforcement, state, and the model loop
have to be tested in isolation (tests cannot call Gemini). Why not more: the
prompt is a string and does not deserve its own file.

## D3. Conversation state contract `[DECIDED]`

```python
@dataclass
class ConversationState:
    verified: bool = False
    patient_id: str | None = None
    intent: str | None = None          # "book" | "cancel" | "escalate"
    reason: str | None = None
    preferences: dict = {}             # provider_id, date_from, date_to as given to the tool
    offered_slots: list[str] = []      # slot ids the tool actually returned, union over turns
    selected_slot: str | None = None
    booked_appointment_id: str | None = None
    escalated: bool = False
    turn_count: int = 0
    flags: list[str] = []              # loop-detected violations, for the Phase 2 scorer
```

Update rules, one function `apply(state, tool_name, args, result)`:

| tool | on success sets |
|---|---|
| verify_patient | verified, patient_id |
| get_available_slots | offered_slots (union), preferences, reason, intent=book |
| book_appointment | selected_slot, booked_appointment_id, intent=book |
| cancel_appointment | booked_appointment_id=None, intent=cancel |
| escalate_to_human | escalated=True, intent=escalate |

`turn_count` is incremented by the loop per user turn. `flags` is appended by
the loop. Everything else moves only inside `apply`.

Invariant (human-stated, Q2): state mutates only inside `apply()`, which runs
only after a tool executes. `apply` may read both the call's arguments and its
result, so intent, reason and preferences populate from get_available_slots
arguments. If the model calls no tool in a turn, state is byte-identical before
and after that turn. There is a test for exactly that.

Why (human reasoning): "no tool, no state change" is what makes the state
auditable, and it collapses to a one-line test.

The state block serialised into every prompt also carries
`today: 2026-10-12 (Monday)` so relative dates like "next Tuesday" resolve
deterministically (Q5).

## D4. Tool contracts `[DECIDED]` (shapes from the brief are `[DIRECTIVE]`)

All dates are ISO `YYYY-MM-DD`, all times ISO local `YYYY-MM-DDTHH:MM`.

```
verify_patient(full_name: str, dob: str)
  -> {verified: bool, patient_id: str | null}
  match: case-insensitive, whitespace-collapsed name AND exact dob.
  On success the backend records patient_id as verified for this session.
  Never returns any other field, so no other patient's data can leak.

get_available_slots(provider_id: str | null, date_from: str, date_to: str, reason: str)
  -> {slots: [{slot_id, provider, start}], truncated: bool}
  Returns the earliest 6 free slots in range (both providers if null).
  `reason` is recorded in the trace but does not filter; see ASSUMPTIONS A3.

book_appointment(patient_id: str, slot_id: str, reason: str)
  -> {ok: true, appointment_id}
   | {ok: false, error: "not_verified" | "slot_taken" | "unknown_slot" | "slot_not_offered"}
  Enforced in backend: patient_id must be verified this session; slot must be free.
  Idempotent on (patient_id, slot_id): same pair returns the same appointment_id.
  "slot_not_offered" is enforced in tools.dispatch using state.offered_slots (Q3).

cancel_appointment(patient_id: str, appointment_id: str)
  -> {ok: true} | {ok: false, error: "not_verified" | "not_found"}
  Frees the slot. Another patient's appointment id returns not_found, never details.

escalate_to_human(reason: str, urgency: "low" | "medium" | "high")
  -> {ok: true, ticket_id}
```

Slot id format `P1-20261012-0900` (provider, date, time). Chosen so the loop can
find slot ids in model text with one regex and compare against offered_slots.
Appointment ids `A-0001`, tickets `T-0001`, sequential per backend instance.

## D5. Deterministic backend `[DECIDED]`

Slots are generated from `data/seed.json`: anchor Monday, 10 business days,
clinic hours, 30-minute grid, and a fixed list of pre-taken slot ids so
`slot_taken` can happen in scenarios. No randomness anywhere, so the same seed
file always gives the same slots. Each chat or eval run gets a fresh backend.

## D6. Agent loop and trace `[DECIDED]`

Per user turn: append user text, call Gemini with tools, execute every function
call it returns (in order), `apply` each result, append results, call again.
Stop when the model returns text with no function calls, or after 4 tool calls,
at which point one final call is made with tools disabled so the turn ends with
text. Flag `tool_cap_hit` if that happens.

State is serialised as JSON and appended to the system instruction on every
call, so the model always sees the current truth and the prompt never has to
carry it in conversation history.

Trace: one JSONL line per event in `runs/<timestamp>.jsonl`. Event types:
`user`, `tool_call`, `tool_result`, `assistant`, `flag`, `state` (snapshot after
each turn). The Phase 2 scorer reads only this file.

Unoffered slot handling is two separate things, both in the trace (Q3):

1. Control: dispatch rejects book_appointment on a slot_id not in
   offered_slots with error `slot_not_offered`. The backend never sees it.
2. Measurement: every assistant message is scanned for slot-id patterns; any id
   not in offered_slots is logged as `unoffered_mention:<id>` in flags and as a
   `flag` event. Text-level, scored in Phase 2.

Why both (human reasoning): a control is the backend refusing, a measurement is
the trace recording the attempt. The eval harness scores attempts, not just
outcomes, so one without the other loses information.

## D7. Scripted mode `[DECIDED]`

`scenarios/smoke.json` is `{"name": "...", "turns": ["patient utterance", ...]}`.
`--script` replays the turns in order and prints the transcript. Phase 2 adds
expectation fields to the same file shape.

## D8. Deliberately missing in v1 `[DIRECTIVE]`

No read-back confirmation before booking. No retry when book returns
slot_taken. The system prompt does not mention either. The eval loop in
Phase 2 is expected to surface these and the fix is the demo of the loop.

## D9. Dependencies `[DECIDED]`

`google-genai`, `anthropic` (Phase 2), `pytest`. `.env` is read by a six-line
parser in `agent/chat.py` instead of adding python-dotenv. uv for env management,
`pyproject.toml` only.

## D10. System prompt scope `[DECIDED]` (reconstructs truncated brief item 4)

Item 4 of the Phase 1 scope arrived cut off. Human confirmed the v1 rules
exactly (Q1): identify and verify before any appointment data; never give
medical advice; emergency symptoms (chest pain, trouble breathing, stroke signs,
suicidal ideation) mean stop scheduling, tell them to call emergency services,
and call escalate_to_human with urgency high; stay in scope; only offer slots
returned by the tool.

v1 deliberately omits read-back confirmation before booking and
retry-on-slot_taken. Both are intentional seeds for the improvement loop: the
Phase 2 harness should find them, and fixing them is the demonstration.

## Reviewer questions, all DECIDED 2026-10-07

- **Q1** Item 4 truncated. DECIDED: reconstruction confirmed, exact rules in D10.
- **Q2** DECIDED: yes, with the invariant stated in D3 and a no-tool-no-change test.
- **Q3** DECIDED: both. Dispatch control plus text-level measurement, see D6.
- **Q4** DECIDED: default accepted, 6 results, reason does not filter.
- **Q5** DECIDED: fixed anchor 2026-10-12, and today is serialised into the state block.
- **Q6** DECIDED: default accepted, key is (patient_id, slot_id).
- **Q7** DECIDED: default accepted, cancel frees the slot.

## AI versus human judgment

- 2026-10-07: AI drafted D2 to D10 and Q1 to Q7 from the brief. No code written.
- 2026-10-07: Human accepted defaults on Q2, Q3, Q4, Q6, Q7 and sharpened Q1,
  Q2, Q3, Q5. Human added the two reasons recorded in D3 and D6 (auditable
  state via "no tool, no state change"; control versus measurement). Human
  confirmed the two deliberate v1 omissions in D10 are seeds for Phase 2.
