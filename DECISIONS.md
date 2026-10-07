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

## D2. Module layout `[PROPOSAL]`

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

## D3. Conversation state contract `[PROPOSAL]`

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

Tension with the brief, needs a decision: the brief says state is updated ONLY
from tool results, but `intent`, `reason` and `preferences` are not in any tool
result. They only exist as the arguments the model passed to a tool. Proposal:
`apply` may read the call's arguments as well as its result, and the rule
becomes "state changes only when a tool call executes". The model's free text
never touches state. See open question Q2.

## D4. Tool contracts `[PROPOSAL]` (shapes from the brief are `[DIRECTIVE]`)

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
  "slot_not_offered" is enforced in tools.dispatch using state.offered_slots. See Q3.

cancel_appointment(patient_id: str, appointment_id: str)
  -> {ok: true} | {ok: false, error: "not_verified" | "not_found"}
  Frees the slot. Another patient's appointment id returns not_found, never details.

escalate_to_human(reason: str, urgency: "low" | "medium" | "high")
  -> {ok: true, ticket_id}
```

Slot id format `P1-20261012-0900` (provider, date, time). Chosen so the loop can
find slot ids in model text with one regex and compare against offered_slots.
Appointment ids `A-0001`, tickets `T-0001`, sequential per backend instance.

## D5. Deterministic backend `[PROPOSAL]`

Slots are generated from `data/seed.json`: anchor Monday, 10 business days,
clinic hours, 30-minute grid, and a fixed list of pre-taken slot ids so
`slot_taken` can happen in scenarios. No randomness anywhere, so the same seed
file always gives the same slots. Each chat or eval run gets a fresh backend.

## D6. Agent loop and trace `[PROPOSAL]`

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

Unoffered slot detection: after each assistant text, regex for slot ids; any id
not in offered_slots appends `unoffered_slot_mentioned:<id>` to flags and
writes a `flag` event.

## D7. Scripted mode `[PROPOSAL]`

`scenarios/smoke.json` is `{"name": "...", "turns": ["patient utterance", ...]}`.
`--script` replays the turns in order and prints the transcript. Phase 2 adds
expectation fields to the same file shape.

## D8. Deliberately missing in v1 `[DIRECTIVE]`

No read-back confirmation before booking. No retry when book returns
slot_taken. The system prompt does not mention either. The eval loop in
Phase 2 is expected to surface these and the fix is the demo of the loop.

## D9. Dependencies `[PROPOSAL]`

`google-genai`, `anthropic` (Phase 2), `pytest`. `.env` is read by a six-line
parser in `agent/chat.py` instead of adding python-dotenv. uv for env management,
`pyproject.toml` only.

## D10. System prompt scope `[PROPOSAL]` (reconstructs truncated brief item 4)

Item 4 of the Phase 1 scope arrived cut off. Reconstructed intent, pending
confirmation (Q1): verify identity before booking or cancelling; if the patient
describes an emergency, call escalate_to_human with urgency high and tell them
to call emergency services; stay in scope (no medical advice, no other
patients); only offer slots the tool returned, by slot id; short turns suited to
voice.

## Open questions for the reviewer

- **Q1** Item 4 of the brief is truncated. Is D10 the intended content?
- **Q2** May `apply` read tool call arguments for intent, reason, preferences
  (D3)? If no, those three fields stay None in v1.
- **Q3** Booking a slot that was never offered: block at dispatch with
  `slot_not_offered` and flag (proposal), or let it reach the backend and only
  flag? The brief says "flags it" for mentions but "cannot book" for the test.
- **Q4** Cap get_available_slots at 6 results and ignore `reason` for
  filtering (D4, A3)?
- **Q5** Fixed anchor date in the seed (A1) rather than today-relative?
- **Q6** Idempotency key is (patient_id, slot_id); a different `reason` on the
  same pair is ignored and the same appointment_id returns. Agree?
- **Q7** Cancel frees the slot for others (A5). Agree?

## AI versus human judgment

- 2026-10-07: AI drafted D2 to D10 and all questions from the brief. No code
  written yet. Human review pending.
